# ClipFinder

Paste a YouTube or Twitch link and get the most exciting moments as vertical
**9:16 clips** (1080×1920), ready for TikTok, Instagram Reels and YouTube Shorts.

## What it does

1. **Downloads a small copy** of the video (360p) with `yt-dlp`, just for analysis.
2. **Scores every half-second** of the video on:
   - loudness compared with the surrounding minute (shouting, laughing, hype)
   - sudden jumps in volume (reactions)
   - how much the picture changes (fast action)
   - YouTube's "most replayed" heatmap, when the video has one
3. **Picks the top moments**, spread across the video, each 10–15 s long with
   the peak about 60% of the way in (build-up before, reaction after).
4. **Downloads only those seconds in HD** (up to 1080p60).
5. **Finds the streamer's webcam**: a face that stays in one place across the
   clip (works with green-screen cutouts as well as boxed cams; in-game faces
   move around and are ignored). It stacks the **webcam on top** and the
   **gameplay below**. If no cam is found, it falls back to a center crop.
6. **Auto captions**: transcribes speech locally with Whisper and burns in
   TikTok-style captions (bold, 1–3 words at a time, current word highlighted).

## Setup

Requires Python 3.10+ (macOS; captions use the system Arial Black font).

```bash
python3 -m venv .env      # or: conda create -p ./.env python=3.12
./.env/bin/python -m pip install -r requirements.txt
```

ffmpeg comes bundled through `imageio-ffmpeg`, so there's nothing else to
install. The first captioned run downloads the Whisper `small` model (~460 MB)
from Hugging Face.

## Use it

Web UI:

```bash
./.env/bin/python app.py      # then open http://localhost:5050
```

Command line:

```bash
./.env/bin/python clipper.py "https://www.twitch.tv/videos/..." --count 5
./.env/bin/python clipper.py video.mp4 --mode blur --no-captions
```

Options: `--count`, `--min` / `--max` (clip length in seconds),
`--mode facecam|crop|blur`, `--cam 0.35` (share of the height for the webcam),
`--no-captions`, `--out` (output folder).

## Files

| File | What it is |
| --- | --- |
| `clipper.py` | Pipeline: download, analyze, score, select, webcam detection, render |
| `captions.py` | Whisper transcription and caption styling (.ass subtitles) |
| `app.py` / `index.html` | Local web UI (Flask) |
| `models/` | YuNet face detector from [OpenCV Zoo](https://github.com/opencv/opencv_zoo) (MIT) |

Only clip videos you own or have permission to use.
