"""
Virality checker for an existing short (TikTok / YouTube Short / Instagram Reel).

Scores one uploaded file (or a link yt-dlp can fetch) with the same model as
generated clips (virality.py: hook / hold / emotion / share / loop, plus the
optional Claude rubric), and adds a format checklist for things the platforms
care about before anyone even watches: vertical framing, length, resolution,
a face early on, pacing, and audio level.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import captions as cap
import clipper
import virality as vir

Progress = clipper.Progress


def _video_info(path: Path) -> dict:
    import cv2
    v = cv2.VideoCapture(str(path))
    info = dict(width=int(v.get(cv2.CAP_PROP_FRAME_WIDTH)), height=int(v.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                fps=float(v.get(cv2.CAP_PROP_FPS) or 0))
    v.release()
    return info


def _face_early(path: Path, seconds: float = 1.5, samples: int = 4) -> bool:
    """Is a face on screen in the opening moments? (strong scroll-stopper)"""
    import cv2
    if not clipper.YUNET.exists():
        return False
    cap_ = cv2.VideoCapture(str(path))
    W = int(cap_.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap_.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not W:
        return False
    scale = 640 / max(W, H)
    dw, dh = int(W * scale), int(H * scale)
    det = cv2.FaceDetectorYN.create(str(clipper.YUNET), "", (dw, dh), 0.7)
    found = False
    for k in range(samples):
        cap_.set(cv2.CAP_PROP_POS_MSEC, 1000 * seconds * k / max(1, samples - 1))
        ok, frame = cap_.read()
        if ok:
            _, faces = det.detect(cv2.resize(frame, (dw, dh)))
            if faces is not None and len(faces):
                found = True
                break
    cap_.release()
    return found


def _check(ok: bool | None, label: str, detail: str) -> dict:
    return {"status": "pass" if ok else ("warn" if ok is None else "fail"), "label": label, "detail": detail}


def format_checks(info: dict, duration: float, sig: clipper.Signals, words: list, face: bool) -> list[dict]:
    w, h = info["width"], info["height"]
    ratio = w / h if h else 0
    checks = []

    if abs(ratio - 9 / 16) < 0.03:
        checks.append(_check(True, "Vertical 9:16", f"{w}×{h}, fills the phone screen"))
    elif ratio < 1:
        checks.append(_check(None, "Vertical, not 9:16", f"{w}×{h}; 9:16 (e.g. 1080×1920) fills the screen with no bars"))
    else:
        checks.append(_check(False, "Not vertical", f"{w}×{h}; horizontal video gets black bars - reframe to 9:16"))

    short_side = min(w, h)
    checks.append(_check(True if short_side >= 1080 else (None if short_side >= 720 else False),
                         "Resolution", f"{short_side}p short side" + ("" if short_side >= 1080 else "; 1080p looks sharper in feeds")))

    if 7 <= duration <= 35:
        checks.append(_check(True, "Length", f"{duration:.0f}s; short clips complete and loop more often"))
    elif duration <= 60:
        checks.append(_check(None, "Length", f"{duration:.0f}s; fine, but completion drops as clips get longer"))
    else:
        checks.append(_check(False, "Length", f"{duration:.0f}s; trim to the strongest 15-35 s for better completion"))

    checks.append(_check(True if face else None, "Face in first second",
                         "A face is on screen right away" if face else "No face at the start; faces stop the scroll"))

    first_word = words[0].start if words else None
    if first_word is None:
        checks.append(_check(None, "Opens with speech", "No speech detected; on-screen text helps viewers follow"))
    else:
        checks.append(_check(first_word <= 1.5, "Opens with speech",
                             f"First words at {first_word:.1f}s" + ("" if first_word <= 1.5 else "; start talking sooner")))

    cuts = int(np.sum(sig.motion > 0.3))                    # big frame changes ≈ cuts / scene changes
    per_min = cuts / max(duration, 1) * 60
    checks.append(_check(True if 6 <= per_min <= 60 else None, "Pacing",
                         f"~{per_min:.0f} visual changes per minute" +
                         ("" if 6 <= per_min <= 60 else ("; add cuts, zooms or text pops" if per_min < 6 else "; very busy, may feel chaotic"))))

    loud = float(np.median(sig.loudness[sig.loudness > -60])) if np.any(sig.loudness > -60) else -90.0
    checks.append(_check(True if loud > -30 else (None if loud > -40 else False), "Audio level",
                         f"{loud:.0f} dBFS typical" + ("" if loud > -30 else "; quiet audio loses viewers, boost it")))
    return checks


def check(source: str, workdir: Path, progress: Progress = clipper._noop, use_llm: bool = True) -> dict:
    """source: a local file path or a TikTok / Shorts / Reels / YouTube link."""
    workdir.mkdir(parents=True, exist_ok=True)
    progress("Loading clip", 0.02)
    src = clipper.fetch(source, workdir / "_download", progress)
    if src.duration > 600:
        raise RuntimeError("This checker is for short clips (up to 10 minutes). Use Generate clips for long videos.")

    progress("Measuring energy, motion and pacing", 0.35)
    sig = clipper.analyze(src, lambda m, f: progress(m, 0.35 + 0.25 * (f - 0.30) / 0.45))
    scores = clipper.score(sig)

    progress("Transcribing speech", 0.62)
    words = cap.transcribe(clipper.FFMPEG, src.path, 0.0, src.duration)

    progress("Checking format and opening frames", 0.75)
    info = _video_info(src.path)
    face = _face_early(src.path)
    checks = format_checks(info, src.duration, sig, words, face)

    progress("Scoring virality", 0.82)
    v = vir.local_metrics(scores, 0.0, src.duration, words)
    if face:
        v.hook = min(100, v.hook + 8)                       # early face = stronger scroll-stopper
    rubric = None
    if use_llm:
        progress("Asking Claude for a rubric score", 0.86)
        rubric = vir.llm_rank(src.title, [{
            "id": 0, "transcript": " ".join(w.text for w in words) or "(no speech)",
            "length_s": round(src.duration, 1), "peak_at_s": v.peak_time,
            "hook": v.hook, "hold": v.hold, "emotion": v.emotion, "loop": v.loop}],
            note="This is a single, already-finished short uploaded for review (not a stream cut). "
                 "There is only one candidate: set comparative_score to your absolute estimate of its viral potential.")
    v = vir.finalize(v, (rubric or {}).get(0))
    # format problems are things the creator can fix before posting
    for c in checks:
        if c["status"] == "fail":
            v.tips.insert(0, f"{c['label']}: {c['detail']}")

    step = max(1, len(scores["total"]) // 300)
    result = {
        "kind": "check",
        "title": src.title,
        "duration": round(src.duration, 2),
        # path the UI can play back through /clips/<job>/<file>
        "file": (str(src.path.resolve().relative_to(workdir.resolve()))
                 if src.path.resolve().is_relative_to(workdir.resolve()) else ""),
        "video": info,
        "virality": v.to_dict(),
        "checks": checks,
        "transcript": " ".join(w.text for w in words),
        "timeline": {"t": [round(float(x), 1) for x in sig.t[::step]],
                     "score": [round(float(x), 2) for x in scores["total"][::step]]},
    }
    (workdir / "result.json").write_text(json.dumps(result, indent=2))
    progress("Done", 1.0)
    return result


if __name__ == "__main__":
    import sys
    res = check(sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else "output/check"))
    v = res["virality"]
    print(f"Virality {v['score']} ({v['grade']})  hook {v['hook']} hold {v['hold']} emotion {v['emotion']} "
          f"share {v['share']} loop {v['loop']}  ~{v['hold_rate']}% hold")
    for c in res["checks"]:
        print(f"  [{c['status']:4s}] {c['label']}: {c['detail']}")
    for t in v["tips"]:
        print("  tip:", t)
