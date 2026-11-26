"""Local web UI for the auto-clipper.  Run:  python app.py  → http://localhost:5050"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import checker
import clipper

ROOT = Path(__file__).parent
JOBS_DIR = ROOT / "jobs"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024      # 500 MB uploads
jobs: dict[str, dict] = {}


def _start(target, *args) -> str:
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = dict(id=job_id, status="running", progress=0.0, message="Starting", created=time.time())
    threading.Thread(target=target, args=(job_id, *args), daemon=True).start()
    return job_id


def _work(job_id: str, params: dict) -> None:
    job = jobs[job_id]

    def progress(msg: str, frac: float) -> None:
        job["message"], job["progress"] = msg, round(frac, 3)

    try:
        job["result"] = clipper.run(
            params["url"], JOBS_DIR / job_id,
            count=params["count"], min_len=params["min_len"], max_len=params["max_len"],
            mode=params["mode"], progress=progress, cam_frac=params["cam_frac"],
            captions=params["captions"], virality=params["virality"],
        )
        job["status"] = "done"
    except Exception as e:  # surface any failure to the UI
        job["status"], job["error"] = "error", str(e)


@app.post("/api/jobs")
def create_job():
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify(error="Paste a YouTube or Twitch link"), 400
    min_len = float(d.get("min_len", 10))
    max_len = max(min_len, float(d.get("max_len", 15)))
    params = dict(url=url, count=max(1, min(int(d.get("count", 5)), 15)),
                  min_len=min_len, max_len=max_len,
                  mode=d.get("mode") if d.get("mode") in ("facecam", "crop", "blur") else "facecam",
                  cam_frac=min(0.5, max(0.25, float(d.get("cam_frac", 0.35)))),
                  captions=bool(d.get("captions", True)),
                  virality=bool(d.get("virality", True)))
    return jsonify(id=_start(_work, params))


def _check_work(job_id: str, source: str) -> None:
    job = jobs[job_id]

    def progress(msg: str, frac: float) -> None:
        job["message"], job["progress"] = msg, round(frac, 3)

    try:
        job["result"] = checker.check(source, JOBS_DIR / job_id, progress)
        job["status"] = "done"
    except Exception as e:
        job["status"], job["error"] = "error", str(e)


@app.post("/api/check")
def create_check():
    """Virality check for one existing short: multipart `file` upload, or form/JSON `url`."""
    f = request.files.get("file")
    if f and f.filename:
        ext = Path(f.filename).suffix.lower()
        if ext not in VIDEO_EXTS:
            return jsonify(error=f"Unsupported file type {ext or '(none)'}; use MP4, MOV, WEBM or MKV"), 400
        job_id = uuid.uuid4().hex[:10]
        dest = JOBS_DIR / job_id / f"upload{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(dest)
        jobs[job_id] = dict(id=job_id, status="running", progress=0.0, message="Starting", created=time.time())
        threading.Thread(target=_check_work, args=(job_id, str(dest)), daemon=True).start()
        return jsonify(id=job_id)
    url = (request.form.get("url") or (request.get_json(silent=True) or {}).get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Upload a video file or paste a TikTok, Shorts or Reels link"), 400
    return jsonify(id=_start(_check_work, url))


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="That file is over 500 MB"), 413


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = jobs.get(job_id)
    saved = JOBS_DIR / job_id / "result.json"
    if job is None and saved.is_file():  # finished in an earlier server run
        import json
        job = dict(id=job_id, status="done", progress=1.0, message="Done",
                   result=json.loads(saved.read_text()))
    return (jsonify(job), 200) if job else (jsonify(error="not found"), 404)


@app.get("/clips/<job_id>/<path:name>")
def clip_file(job_id: str, name: str):
    return send_from_directory(JOBS_DIR / job_id, name,
                               as_attachment=request.args.get("dl") == "1")


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


if __name__ == "__main__":
    JOBS_DIR.mkdir(exist_ok=True)
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
