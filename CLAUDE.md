# CLAUDE.md

ClipFinder turns long YouTube/Twitch videos into captioned 9:16 shorts and scores them for virality.
It is a local Flask app plus a CLI, written in flat top-level Python modules (no package).

## Commands

Everything goes through [uv](https://docs.astral.sh/uv/). Don't use pip or `requirements.txt`.

```bash
uv sync                               # install app + dev deps into .venv
uv run app.py                         # web UI on http://localhost:5050
uv run clipper.py <url-or-file> --count 5 [--mode facecam|crop|blur] [--no-captions] [--no-virality]
uv run pytest                         # tests (fast, no network, no video files)
uv run ruff check . [--fix]           # lint
uv run ruff format .                  # format
uv run pre-commit run --all-files     # TruffleHog + Ruff + hygiene hooks
uv add <pkg>  /  uv add --dev <pkg>   # add a dependency (updates pyproject.toml + uv.lock)
```

## Architecture

- `clipper.py`: the pipeline. `fetch` downloads a 360p copy with yt-dlp, then `analyze` makes one ffmpeg pass
  for loudness and scene change per 0.5 s bin. `score` computes local z-scores weighted by `WEIGHTS` and
  `select` picks the top peaks and builds `Clip`s. `render` fetches the HD section, then
  `find_webcam` (YuNet) and the ffmpeg filtergraph produce 1080×1920. `rank_by_virality` follows.
  `run()` orchestrates and writes `result.json`.
- `captions.py`: faster-whisper transcription → `.ass` subtitles burned in by ffmpeg.
- `virality.py`: 0–99 virality score (hook / hold / emotion / share / loop) plus an optional Claude rubric
  (model `claude-opus-5-5`), which falls back to local heuristics when there are no Anthropic credentials.
- `checker.py`: virality check and format checklist for one uploaded short.
- `app.py` + `index.html`: Flask API (`/api/jobs`, `/api/check`, `/clips/...`) plus a single-page
  Bootstrap UI. Jobs run in background threads; state lives in memory and in `jobs/<id>/result.json`.
- `models/face_detection_yunet_2023mar.onnx`: committed face detector (MIT).

## Conventions and gotchas

- ffmpeg comes from `imageio-ffmpeg`. Use `clipper.FFMPEG`, never a system `ffmpeg`. On import, `clipper.py`
  symlinks it into `.ffbin/` and prepends that to `PATH` so yt-dlp can find it.
- Heavy imports (`yt_dlp`, `cv2`, `faster_whisper`, `anthropic`) are deferred into functions to keep startup
  and tests fast. Keep it that way.
- Temp dirs for ffmpeg filtergraphs must not contain spaces or colons (see `ass_filter`).
- This is an Intel Mac. `tool.uv.required-environments` in `pyproject.toml` keeps the lock on versions with
  Intel macOS wheels (e.g. onnxruntime 1.23.x). Don't remove it.
- `jobs/` and `output/` hold downloaded and rendered video. They are git-ignored; never commit them.
- Tests cover pure functions only (scoring, selection, caption chunking/ASS). Use synthetic
  `numpy` signals, not real videos or network calls.
- The README images live in `docs/`. The demo media comes from *Big Buck Bunny* (CC BY 3.0); keep the credit
  line if you replace them.

## Safety

- Never commit secrets. `ANTHROPIC_API_KEY` belongs in the environment or a git-ignored `.env.*` file.
  TruffleHog runs as a pre-commit and pre-push hook and in CI. Don't bypass it with `--no-verify`.
- Don't `git push` or open PRs without the user's explicit permission.
