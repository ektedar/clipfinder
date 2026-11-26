"""
Auto captions: transcribe a clip's speech locally with Whisper (faster-whisper)
and turn it into TikTok-style burned-in subtitles (.ass for ffmpeg/libass):

  * 1-3 words on screen at a time, bold uppercase, thick black outline
  * the word being spoken is highlighted
  * each new phrase "pops" in
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODEL_NAME = "small"          # "base" is ~2x faster, slightly less accurate
FONT = "Arial Black"
FONTS_DIR = "/System/Library/Fonts/Supplemental"   # macOS; fontconfig falls back elsewhere
HIGHLIGHT = "&H0000E6FF&"     # ASS colours are &HBBGGRR& -> warm yellow
_model = None
# Whisper "hallucinates" on music, game audio and silence: it was trained on a lot
# of YouTube subtitles, so with no clear speech it tends to output stock phrases
# such as "you", "Thank you.", "Thanks for watching!" or "[Music]". Those would be
# burned into the clip as fake captions. transcribe() drops a segment only when
# its *whole* text is one of these phrases AND Whisper itself was unsure (high
# no-speech probability or low confidence), so a streamer really saying
# "thank you" mid-sentence is kept.
_PHANTOM = re.compile(r"(you|thank you\.?|thanks for watching!?|bye\.?|\.+|music|\[music\]|subtitles by.*)[.!]?")


@dataclass
class Word:
    text: str
    start: float
    end: float


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    return _model


def transcribe(ffmpeg: str, video: Path, start: float, length: float) -> list[Word]:
    """Speech in video[start:start+length] -> words with times relative to `start`."""
    pcm = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(video),
         "-t", f"{length:.2f}", "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True).stdout
    audio = np.frombuffer(pcm, np.float32)
    if audio.size < 16000 * 0.5:
        return []
    segs, _ = _get_model().transcribe(audio, word_timestamps=True, vad_filter=True,
                                      condition_on_previous_text=False)
    words = []
    for s in segs:
        # Whisper "hears" phantom phrases in music/noise ("you", "thank you")
        if _PHANTOM.fullmatch(s.text.strip().lower()) and (s.no_speech_prob > 0.2 or s.avg_logprob < -0.6):
            continue
        for w in s.words or []:
            t = w.word.strip()
            if t and w.probability >= 0.3:
                words.append(Word(t, max(0.0, w.start), min(length, w.end)))
    return words


def _chunks(words: list[Word], max_words: int = 3, max_chars: int = 18) -> list[list[Word]]:
    """Group words into short phrases, breaking on pauses and punctuation."""
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words
                    or sum(len(x.text) + 1 for x in cur) + len(w.text) > max_chars
                    or w.start - cur[-1].end > 0.6
                    or re.search(r"[.!?,]$", cur[-1].text)):
            out.append(cur); cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _ts(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(s: str) -> str:
    return s.upper().replace("{", "(").replace("}", ")").replace("\\", "")


def build_ass(words: list[Word], out: Path, y: int, width: int = 1080, height: int = 1920,
              font_size: int = 84) -> Path | None:
    """Write an .ass subtitle file; captions are centred horizontally at height y."""
    if not words:
        return None
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,7,3,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    chunks = _chunks(words)
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0].start if ci + 1 < len(chunks) else ch[-1].end + 0.6
        chunk_end = min(nxt, ch[-1].end + 0.6)
        for wi, w in enumerate(ch):
            s = w.start
            e = ch[wi + 1].start if wi + 1 < len(ch) else chunk_end
            if e - s < 0.05:
                continue
            parts = [(f"{{\\c{HIGHLIGHT}}}{_clean(x.text)}{{\\c&H00FFFFFF&}}" if j == wi else _clean(x.text))
                     for j, x in enumerate(ch)]
            pop = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)" if wi == 0 else ""
            lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\pos({width // 2},{y}){pop}}}"
                         + " ".join(parts))
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out


def ass_filter(path: Path) -> str:
    """ffmpeg filter that burns the subtitles in (path must have no spaces/colons)."""
    return f"ass={path}:fontsdir={FONTS_DIR}"
