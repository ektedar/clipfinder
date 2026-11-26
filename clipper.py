"""
Auto-clipper: find the most exciting moments in a YouTube/Twitch video
and render them as vertical 9:16 clips (Reels / TikTok / Shorts).

Pipeline
  1. download   – yt-dlp (or use a local file path directly)
  2. analyze    – one ffmpeg pass extracts:
                    * audio loudness per 0.5 s bin   (shouting, laughing, hype)
                    * visual change per 0.5 s bin    (fast action, movement)
                  plus YouTube's "most replayed" heatmap when available
  3. score      – each signal is turned into a "how unusual is this vs. the
                  surrounding minute" z-score, weighted, smoothed
  4. select     – top N peaks, spaced apart, each expanded to 10–15 s
  4b. virality – 3x candidates scored on hook / hold / emotion / share / loop
                  (+ optional Claude rubric), top N kept (see virality.py)
  5. render     – downloads just that section in HD, finds the streamer's
                  webcam (a face that stays put), and stacks cam-on-top /
                  gameplay-below at 1080x1920 (or crop / blur fallback)

CLI:  python clipper.py <url-or-file> [--count 5] [--mode crop|blur]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# yt-dlp only recognises a binary literally named "ffmpeg", so expose the
# bundled one under that name in a private folder.
_FFDIR = Path(__file__).resolve().parent / ".ffbin"
_FFDIR.mkdir(exist_ok=True)
if not (_FFDIR / "ffmpeg").exists():
    (_FFDIR / "ffmpeg").symlink_to(FFMPEG)
# yt-dlp's partial-download check ignores ffmpeg_location and only searches PATH
os.environ["PATH"] = str(_FFDIR) + os.pathsep + os.environ.get("PATH", "")
BIN = 0.5          # seconds per analysis bin
SR = 8000          # audio sample rate used for analysis

Progress = Callable[[str, float], None]   # (message, fraction 0..1)


def _noop(msg: str, frac: float) -> None:
    print(f"[{frac*100:5.1f}%] {msg}")


# --------------------------------------------------------------------------
# 1. Download
# --------------------------------------------------------------------------
@dataclass
class Source:
    path: Path                  # file used for analysis (low-res copy for URLs)
    title: str
    duration: float
    heatmap: list = field(default_factory=list)   # YouTube "most replayed"
    url: str = ""               # set for online sources -> clips fetched in HD


HD_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]/b"
# Analysis only needs rough audio + motion, so grab a small copy. This turns a
# multi-GB 3-hour 1080p60 stream into a few hundred MB and decodes ~10x faster.
LOW_FORMAT = "b[height<=360][height>=160]/bv*[height<=360]+ba/w"


def _ydl_opts(fmt: str, outtmpl: str, hook=None) -> dict:
    o = {"format": fmt, "merge_output_format": "mp4", "outtmpl": outtmpl,
         "ffmpeg_location": str(_FFDIR), "quiet": True, "no_warnings": True, "noprogress": True,
         "concurrent_fragment_downloads": 8}
    if hook:
        o["progress_hooks"] = [hook]
    return o


def fetch(url_or_path: str, workdir: Path, progress: Progress = _noop) -> Source:
    p = Path(url_or_path).expanduser()
    if p.exists():
        return Source(path=p, title=p.stem, duration=_probe_duration(p))

    import yt_dlp

    workdir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if total:
            frac = d.get("downloaded_bytes", 0) / total
        elif d.get("fragment_count"):                      # HLS (Twitch)
            frac = (d.get("fragment_index") or 0) / d["fragment_count"]
        else:
            return
        progress("Downloading low-res copy for analysis", 0.30 * min(1.0, frac))

    # reuse a previous analysis download of the same URL (re-runs are instant)
    cache = workdir / "info.json"
    if cache.exists():
        meta = json.loads(cache.read_text())
        if meta.get("source") == url_or_path and Path(meta["path"]).exists():
            progress("Using cached download", 0.30)
            return Source(path=Path(meta["path"]), title=meta["title"], duration=meta["duration"],
                          heatmap=meta["heatmap"], url=meta["url"])

    progress("Fetching video info", 0.01)
    with yt_dlp.YoutubeDL(_ydl_opts(LOW_FORMAT, str(workdir / "analysis.%(ext)s"), hook)) as ydl:
        info = ydl.extract_info(url_or_path, download=True)
    cands = sorted(workdir.glob("analysis.*"), key=lambda f: f.stat().st_size, reverse=True)
    if not cands:
        raise RuntimeError("Download failed")
    path = cands[0]
    src = Source(
        path=path,
        title=info.get("title") or "video",
        duration=float(info.get("duration") or _probe_duration(path)),
        heatmap=info.get("heatmap") or [],
        url=info.get("webpage_url") or url_or_path,
    )
    cache.write_text(json.dumps({"source": url_or_path, "path": str(path), "title": src.title,
                                 "duration": src.duration, "heatmap": src.heatmap, "url": src.url}))
    return src


def fetch_section(src: Source, start: float, end: float, out: Path) -> Path:
    """Download just [start, end] of the original in HD."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    opts = _ydl_opts(HD_FORMAT, str(out.with_suffix(".%(ext)s")))
    opts["download_ranges"] = download_range_func(None, [(start, end)])
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([src.url])
    cands = [f for f in out.parent.glob(out.stem + ".*") if f.suffix not in (".part", ".ytdl")]
    if not cands:
        raise RuntimeError("HD section download failed")
    return max(cands, key=lambda f: f.stat().st_size)


def _probe_duration(path: Path) -> float:
    out = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
    if not m:
        raise RuntimeError(f"Could not read video duration for {path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


# --------------------------------------------------------------------------
# 2. Analyze (single ffmpeg pass: audio samples -> stdout, scene scores -> file)
# --------------------------------------------------------------------------
@dataclass
class Signals:
    t: np.ndarray          # bin start times
    loudness: np.ndarray   # dBFS per bin
    motion: np.ndarray     # 0..1 mean frame-difference per bin
    heat: np.ndarray       # 0..1 YouTube replay heat (zeros if absent)


def analyze(src: Source, progress: Progress = _noop) -> Signals:
    tmp = Path(tempfile.mkdtemp(prefix="clipper_"))  # no spaces -> safe in filtergraph
    scene_file = tmp / "scene.txt"
    hop = int(SR * BIN)

    cmd = [
        FFMPEG, "-hide_banner", "-nostats", "-loglevel", "error",
        "-i", str(src.path),
        # audio: mono 8 kHz raw PCM to stdout
        "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1",
        # video: 4 fps, tiny, scene-change score per frame written to a file
        "-map", "0:v:0", "-an",
        "-vf", f"fps=4,scale=160:-2,select='gte(scene,0)',"
               f"metadata=print:key=lavfi.scene_score:file={scene_file}",
        "-f", "null", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    total_bins = max(1, int(src.duration / BIN))
    loud = []
    buf = b""
    while True:
        chunk = proc.stdout.read(hop * 2 * 200)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // (hop * 2)
        if n:
            a = np.frombuffer(buf[: n * hop * 2], dtype=np.int16).astype(np.float32) / 32768.0
            buf = buf[n * hop * 2:]
            rms = np.sqrt(np.mean(a.reshape(n, hop) ** 2, axis=1) + 1e-10)
            loud.extend(20 * np.log10(rms + 1e-6))
            progress("Listening for hype moments + watching for action",
                     0.30 + 0.45 * min(1.0, len(loud) / total_bins))
    err = proc.stderr.read().decode(errors="ignore")
    proc.wait()
    if proc.returncode != 0 and not loud:
        raise RuntimeError(f"ffmpeg analysis failed: {err[-500:]}")

    loudness = np.array(loud, dtype=np.float32)
    nb = len(loudness)
    t = np.arange(nb) * BIN

    # motion: average scene score of the frames that fall inside each bin
    motion = np.zeros(nb, dtype=np.float32)
    counts = np.zeros(nb, dtype=np.float32)
    if scene_file.exists():
        cur_t = None
        for line in scene_file.read_text().splitlines():
            if line.startswith("frame:"):
                m = re.search(r"pts_time:([\d.]+)", line)
                cur_t = float(m.group(1)) if m else None
            elif line.startswith("lavfi.scene_score=") and cur_t is not None:
                i = int(cur_t / BIN)
                if 0 <= i < nb:
                    motion[i] += float(line.split("=", 1)[1])
                    counts[i] += 1
    motion = motion / np.maximum(counts, 1)

    heat = np.zeros(nb, dtype=np.float32)
    for seg in src.heatmap:
        a, b = int(seg["start_time"] / BIN), int(seg["end_time"] / BIN) + 1
        heat[a:b] = float(seg.get("value", 0))

    return Signals(t=t, loudness=loudness, motion=motion, heat=heat)


# --------------------------------------------------------------------------
# 3. Score
# --------------------------------------------------------------------------
def _moving(x: np.ndarray, win: int) -> np.ndarray:
    win = max(1, min(win, len(x)))
    k = np.ones(win) / win
    pad = win // 2
    xp = np.pad(x, (pad, win - 1 - pad), mode="edge")
    return np.convolve(xp, k, mode="valid")


def _local_z(x: np.ndarray, win_s: float = 90.0) -> np.ndarray:
    """How unusual each bin is compared to the surrounding ~minute and a half.
    Local (not global) so a loud intro or a quiet segment don't dominate."""
    w = int(win_s / BIN)
    mu = _moving(x, w)
    sd = np.sqrt(np.maximum(_moving((x - mu) ** 2, w), 1e-6))
    gsd = np.std(x) + 1e-6
    return (x - mu) / np.maximum(sd, 0.35 * gsd)   # floor sd so flat parts don't explode


WEIGHTS = {"audio": 1.0, "burst": 0.5, "motion": 0.6, "heat": 0.7}


def score(sig: Signals) -> dict[str, np.ndarray]:
    loud = np.maximum(sig.loudness, -70.0)          # treat silence as -70 dB
    audio = np.clip(_local_z(loud), -2, 4)
    # burst: sudden jumps in loudness (a shout after calm talk)
    rise = np.maximum(0, loud - _moving(loud, int(4 / BIN)))
    burst = np.clip(_local_z(rise), -2, 4)
    motion = np.clip(_local_z(sig.motion), -2, 4)
    heat = sig.heat
    # heatmap is a coarse, whole-video signal: compare locally too so it nudges
    # rather than dominates (it tends to pile up on the ending otherwise)
    heat_z = np.clip(_local_z(heat, 180.0), -2, 3) if heat.any() else np.zeros_like(heat)

    parts = {"audio": audio, "burst": burst, "motion": motion, "heat": heat_z}
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    total = _moving(total, int(3 / BIN))            # 3 s smoothing -> moments, not blips
    return {"total": total, **{k: _moving(v, int(3 / BIN)) for k, v in parts.items()}}


# --------------------------------------------------------------------------
# 4. Select clips
# --------------------------------------------------------------------------
REASONS = {
    "audio": "Loud / hype audio",
    "burst": "Sudden reaction",
    "motion": "Fast on-screen action",
    "heat": "Most-replayed by viewers",
}


@dataclass
class Clip:
    index: int
    start: float
    end: float
    peak: float
    score: float
    reason: str
    file: str = ""
    layout: str = ""
    caption: str = ""
    virality: dict = field(default_factory=dict)

    @property
    def length(self) -> float:
        return self.end - self.start


def select(scores: dict[str, np.ndarray], duration: float, count: int = 5,
           min_len: float = 10.0, max_len: float = 15.0) -> list[Clip]:
    total = scores["total"].copy()
    nb = len(total)
    # don't clip the very first/last seconds (intros, end screens)
    head = int(min(8.0, duration * 0.03) / BIN)
    tail = int(min(30.0, duration * 0.04) / BIN)       # skip outros / end cards
    total[:head] = -np.inf
    total[max(0, nb - tail):] = -np.inf

    clips: list[Clip] = []
    avail = total.copy()
    for _ in range(count):
        i = int(np.argmax(avail))
        if not np.isfinite(avail[i]) or avail[i] < 0.3:   # nothing exciting left
            break
        peak_val = total[i]

        # grow the window while the moment stays "hot"
        thr = 0.5 * peak_val
        lo = hi = i
        while lo > 0 and total[lo - 1] > thr and (i - lo) * BIN < max_len * 0.6:
            lo -= 1
        while hi < nb - 1 and total[hi + 1] > thr and (hi - i) * BIN < max_len * 0.6:
            hi += 1
        length = float(np.clip((hi - lo + 1) * BIN + 4.0, min_len, max_len))

        # put the peak ~60% in: build-up before, payoff/reaction after
        start = i * BIN - 0.6 * length
        start = float(np.clip(start, 0, max(0.0, duration - length)))
        end = min(duration, start + length)

        dominant = max(REASONS, key=lambda k: WEIGHTS[k] * scores[k][i])
        clips.append(Clip(index=0, start=round(start, 2), end=round(end, 2),
                          peak=round(i * BIN, 2), score=round(float(peak_val), 2),
                          reason=REASONS[dominant]))

        # suppress neighbourhood so clips don't overlap
        # spread clips across the video rather than bunching on one segment
        gap = int(max(max_len + 5, duration / (count * 3)) / BIN)
        avail[max(0, i - gap): i + gap] = -np.inf

    clips.sort(key=lambda c: c.start)
    for n, c in enumerate(clips, 1):
        c.index = n
    return clips


# --------------------------------------------------------------------------
# 5a. Webcam detection
# --------------------------------------------------------------------------
YUNET = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"


def find_webcam(video: Path, start: float, length: float, samples: int = 12):
    """Find the streamer's facecam in video[start:start+length].

    Streamer cams are often green-screen cutouts with no border, so we look for
    a *face* that stays in the same place in most sampled frames. In-game
    characters move around; the webcam doesn't.
    Returns (face_box, (W, H)) in source pixels, or (None, (W, H)).
    """
    import cv2

    cap = cv2.VideoCapture(str(video))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not YUNET.exists() or not W:
        cap.release()
        return None, (W, H)
    scale = 640 / W
    dw, dh = 640, int(round(H * scale))
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (dw, dh), 0.7)

    dets = []   # (frame_idx, cx, cy, w, h) in source pixels
    used = 0
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_MSEC, 1000 * (start + length * (k + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        used += 1
        _, faces = det.detect(cv2.resize(frame, (dw, dh)))
        for f in (faces if faces is not None else []):
            x, y, w, h = (float(v) / scale for v in f[:4])
            dets.append((k, x + w / 2, y + h / 2, w, h))
    cap.release()
    if not used or not dets:
        return None, (W, H)

    # group detections that sit in (roughly) the same spot across frames
    clusters: list[list] = []
    for d in dets:
        for cl in clusters:
            mx = np.median([e[1] for e in cl]); my = np.median([e[2] for e in cl])
            mw = np.median([e[3] for e in cl])
            if abs(d[1] - mx) < 0.6 * mw and abs(d[2] - my) < 0.6 * mw:
                cl.append(d); break
        else:
            clusters.append([d])

    def frames(cl): return len({e[0] for e in cl})
    stable = [cl for cl in clusters if frames(cl) >= max(2, 0.4 * used)]
    if not stable:
        return None, (W, H)
    # most persistent first, then the biggest face (main cam > co-streamer cams)
    best = max(stable, key=lambda cl: (frames(cl), np.median([e[3] * e[4] for e in cl])))
    cx, cy, w, h = (float(np.median([e[i] for e in best])) for i in (1, 2, 3, 4))
    return (cx - w / 2, cy - h / 2, w, h), (W, H)


# --------------------------------------------------------------------------
# 5b. Render 9:16
# --------------------------------------------------------------------------
OUT_W, OUT_H = 1080, 1920


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _facecam_graph(face, W: int, H: int, cam_frac: float) -> str:
    """Webcam on top (cam_frac of the height), gameplay centre-crop below."""
    top_h = _even(OUT_H * cam_frac)
    bot_h = OUT_H - top_h

    # top: frame the head + shoulders around the detected face
    fx, fy, fw, fh = face
    aspect = OUT_W / top_h
    cw = min(W, fw * 3.6)
    ch = cw / aspect
    if ch > H:
        ch, cw = H, H * aspect
    cx, cy = fx + fw / 2, fy + fh * 0.35          # headroom above, shoulders below
    x0 = min(max(0, cx - cw / 2), W - cw)
    y0 = min(max(0, cy - ch / 2), H - ch)

    # bottom: centre of the gameplay at the remaining aspect ratio
    b_aspect = OUT_W / bot_h
    gw = min(W, H * b_aspect)
    gh = min(H, gw / b_aspect)
    gx, gy = (W - gw) / 2, (H - gh) / 2

    return (
        "[0:v]split[a][b];"
        f"[a]crop={_even(cw)}:{_even(ch)}:{int(x0)}:{int(y0)},scale={OUT_W}:{top_h},setsar=1[top];"
        f"[b]crop={_even(gw)}:{_even(gh)}:{int(gx)}:{int(gy)},scale={OUT_W}:{bot_h},setsar=1[bot];"
        "[top][bot]vstack[v]"
    )


def _filter(mode: str) -> tuple[str, str]:
    """Return (flag, filtergraph) for the simple reframing modes."""
    if mode == "blur":
        # whole frame centred, blurred zoomed copy filling the background
        return "-filter_complex", (
            "[0:v]split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:4[bg];"
            "[b]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
        )
    # default: centre crop to 9:16
    return "-vf", "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920,setsar=1"


def render(src: Source, clips: list[Clip], outdir: Path, mode: str = "facecam",
           progress: Progress = _noop, cam_frac: float = 0.35,
           fallback: str = "crop", captions: bool = True) -> list[Clip]:
    """mode: 'facecam' (cam on top if one is found, else `fallback`), 'crop' or 'blur'."""
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="clipper_ass_"))  # no spaces -> safe in filtergraph
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", src.title).strip("_")[:40] or "clip"
    for n, c in enumerate(clips):
        frac = 0.80 + 0.19 * n / max(1, len(clips))
        out = outdir / f"{slug}_{c.index:02d}_{int(c.start)}s.mp4"
        inp, ss, hd = src.path, c.start, None
        if src.url:
            # grab a padded HD section, then trim precisely below
            pad = 2.0
            s0 = max(0.0, c.start - pad)
            progress(f"Downloading HD footage for clip {c.index}/{len(clips)}", frac)
            hd = fetch_section(src, s0, c.end + pad, outdir / "_download" / f"hd_{c.index:02d}")
            inp, ss = hd, c.start - s0

        c.layout = mode
        if mode == "facecam":
            progress(f"Looking for a webcam in clip {c.index}/{len(clips)}", frac)
            face, (W, H) = find_webcam(inp, ss, c.length)
            if face is not None:
                flag, graph = "-filter_complex", _facecam_graph(face, W, H, cam_frac)
            else:
                c.layout = fallback
                flag, graph = _filter(fallback)
        else:
            flag, graph = _filter(mode)

        c.caption = ""
        if captions:
            import captions as cap
            progress(f"Transcribing speech for clip {c.index}/{len(clips)}", frac)
            words = getattr(c, "_words", None)
            if words is None:
                words = cap.transcribe(FFMPEG, inp, ss, c.length)
            c.caption = " ".join(w.text for w in words)
            # webcam layout: sit on the seam between cam and gameplay;
            # otherwise lower-middle, clear of the TikTok/Reels UI at the bottom
            y = _even(OUT_H * cam_frac) if c.layout == "facecam" else int(OUT_H * 0.68)
            ass = cap.build_ass(words, tmp / f"clip{c.index}.ass", y=y)
            if ass:
                if flag == "-vf":
                    graph += "," + cap.ass_filter(ass)
                else:                                   # graph ends with "[v]"
                    graph = graph[:-3] + "," + cap.ass_filter(ass) + "[v]"

        progress(f"Rendering clip {c.index}/{len(clips)}", frac)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{ss:.2f}", "-i", str(inp), "-t", f"{c.length:.2f}",
               flag, graph]
        if flag == "-filter_complex":
            cmd += ["-map", "[v]", "-map", "0:a:0?"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Render failed for clip {c.index}: {r.stderr[-500:]}")
        if hd is not None:
            hd.unlink(missing_ok=True)
        c.file = out.name
    return clips


# --------------------------------------------------------------------------
# 6. Virality scoring (see virality.py)
# --------------------------------------------------------------------------
def _snap_to_speech(words, start: float, end: float, min_len: float, max_len: float):
    """Don't open or close a clip mid-word. `words` are absolute-time Word objects."""
    for w in words:                                   # word straddling the start
        if w.start < start < w.end and start - w.start <= 1.5:
            start = max(0.0, w.start - 0.1)
            break
    for w in words:                                   # word straddling the end
        if w.start < end < w.end:
            end = w.end + 0.25 if (w.end + 0.25 - start) <= max_len + 1.0 else w.start - 0.05
            break
    if end - start < min_len:
        end = start + min_len
    return round(start, 2), round(end, 2)


def rank_by_virality(src: Source, scores: dict, cands: list[Clip], count: int,
                     min_len: float, max_len: float, progress: Progress = _noop) -> list[Clip]:
    """Score every candidate (Higgsfield-style signals + SupoClip-style Claude rubric)
    and keep the `count` most viral, best first."""
    import captions as cap
    import virality as vir

    local = {}
    for n, c in enumerate(cands):
        progress(f"Scoring virality: candidate {n + 1}/{len(cands)}", 0.76 + 0.03 * n / len(cands))
        # transcribe a padded window so clip edges can snap to whole words
        w0 = max(0.0, c.start - 2.0)
        words = [cap.Word(w.text, w.start + w0, w.end + w0)
                 for w in cap.transcribe(FFMPEG, src.path, w0, c.length + 4.0)]
        c.start, c.end = _snap_to_speech(words, c.start, min(src.duration, c.end), min_len, max_len)
        c.end = min(c.end, src.duration)
        rel = [cap.Word(w.text, w.start - c.start, w.end - c.start)
               for w in words if w.start >= c.start - 0.05 and w.end <= c.end + 0.05]
        c._words = rel
        local[n] = vir.local_metrics(scores, c.start, c.end, rel)

    progress("Ranking candidates with Claude", 0.79)
    items = [{"id": n, "transcript": " ".join(w.text for w in c._words) or "(no speech)",
              "length_s": round(c.length, 1), "peak_at_s": local[n].peak_time,
              "hook": local[n].hook, "hold": local[n].hold, "emotion": local[n].emotion,
              "loop": local[n].loop} for n, c in enumerate(cands)]
    rubric = vir.llm_rank(src.title, items) or {}

    for n, c in enumerate(cands):
        c.virality = vir.finalize(local[n], rubric.get(n)).to_dict()
    best = sorted(cands, key=lambda c: c.virality["score"], reverse=True)[:count]
    for rank, c in enumerate(best, 1):
        c.index = rank
    return best


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run(url_or_path: str, outdir: Path, count: int = 5, min_len: float = 10, max_len: float = 15,
        mode: str = "facecam", progress: Progress = _noop, cam_frac: float = 0.35,
        captions: bool = True, virality: bool = True) -> dict:
    src = fetch(url_or_path, outdir / "_download", progress)
    progress("Analyzing audio and video", 0.30)
    sig = analyze(src, progress)
    progress("Scoring moments", 0.76)
    scores = score(sig)
    # with virality on, over-generate candidates and keep the most viral ones
    clips = select(scores, src.duration, count * 3 if virality else count, min_len, max_len)
    if not clips:
        raise RuntimeError("No stand-out moments found – try a longer or livelier video.")
    if virality:
        clips = rank_by_virality(src, scores, clips, count, min_len, max_len, progress)
    render(src, clips, outdir, mode, progress, cam_frac=cam_frac, captions=captions)

    # downsample the timeline for the UI chart (~600 points)
    step = max(1, len(scores["total"]) // 600)
    result = {
        "title": src.title,
        "duration": src.duration,
        "used_heatmap": bool(src.heatmap),
        "used_llm": any(c.virality.get("used_llm") for c in clips),
        "timeline": {
            "t": [round(float(x), 1) for x in sig.t[::step]],
            "score": [round(float(x), 2) for x in scores["total"][::step]],
        },
        "clips": [asdict(c) | {"length": round(c.length, 2)} for c in clips],
    }
    (outdir / "result.json").write_text(json.dumps(result, indent=2))
    progress("Done", 1.0)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="YouTube/Twitch URL or a local video file")
    ap.add_argument("--out", default="output", help="output folder")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--min", type=float, default=10.0, dest="min_len")
    ap.add_argument("--max", type=float, default=15.0, dest="max_len")
    ap.add_argument("--mode", choices=["facecam", "crop", "blur"], default="facecam",
                    help="facecam = webcam stacked on top when one is detected (falls back to crop)")
    ap.add_argument("--cam", type=float, default=0.35, help="share of the height for the webcam (0.25-0.5)")
    ap.add_argument("--no-captions", action="store_true", help="skip Whisper auto-captions")
    ap.add_argument("--no-virality", action="store_true", help="skip virality scoring / ranking")
    a = ap.parse_args()
    res = run(a.source, Path(a.out), a.count, a.min_len, a.max_len, a.mode, cam_frac=a.cam,
              captions=not a.no_captions, virality=not a.no_virality)
    for c in res["clips"]:
        v = c.get("virality") or {}
        if v:
            print(f"   virality {v['score']:2d} ({v['grade']})  hook {v['hook']} hold {v['hold']} "
                  f"emotion {v['emotion']} share {v['share']} loop {v['loop']}  "
                  f"hold-rate ~{v['hold_rate']}%  {v.get('title','')}")
        print(f"#{c['index']}  {c['start']:7.1f}s → {c['end']:7.1f}s  score {c['score']:5.2f}  {c['reason']:26s}  [{c['layout']}]  {c['file']}")
