#!/usr/bin/env python3
"""Local web interface for generating Shorts with the existing CLI backend."""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
JOBS_ROOT = Path(os.environ.get("STREAM_TO_SHORTS_JOBS", ROOT / "jobs")).resolve()
MAX_UPLOAD_BYTES = int(os.environ.get("STREAM_TO_SHORTS_MAX_UPLOAD_GB", "100")) * 1024**3
MAX_LOG_LINES = 400
JOBS = {}
JOBS_LOCK = threading.Lock()
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com",
                 "twitch.tv", "www.twitch.tv", "kick.com", "www.kick.com"}


def public_video_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("Use a YouTube, Twitch, or Kick video/VOD link.")
    return value


def create_job(source, options):
    job_id = uuid.uuid4().hex[:12]
    folder = JOBS_ROOT / job_id
    folder.mkdir(parents=True, exist_ok=False)
    output = folder / "clips"
    output.mkdir()
    command = [sys.executable, str(ROOT / "clipper.py"), source,
               "--count", str(options["count"]),
               "--length", str(options["length"]),
               "--min-length", str(options["min_length"]),
               "--reframe", options["reframe"], "--out", str(output)]
    if options.get("visual_scan"):
        command.append("--visual-scan")
    if options.get("no_transcribe"):
        command.append("--no-transcribe")
    with JOBS_LOCK:
        JOBS[job_id] = make_job_record(job_id, folder, options, source_is_url=True)
    thread = threading.Thread(target=run_job, args=(job_id, command, folder), daemon=True)
    thread.start()
    return job_id


def make_job_record(job_id, folder, options, source_is_url):
    steps = []
    if source_is_url:
        steps.append({"id": "download", "label": "Download source video"})
    steps.append({"id": "audio", "label": "Analyze audio and find reactions"})
    if options.get("visual_scan"):
        steps.append({"id": "visual", "label": "Scan the video for visual moments"})
    steps.extend([
        {"id": "transcript", "label": "Transcribe promising sections"},
        {"id": "render", "label": "Select moments and render shorts"},
    ])
    return {"id": job_id, "status": "queued", "stage": "Queued", "stage_code": "queued",
            "steps": steps, "logs": [], "clips": [], "error": None,
            "created": int(time.time() * 1000), "folder": str(folder)}


def run_job(job_id, command, folder):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["stage"] = "Starting analysis"
        job["stage_code"] = "download" if any(x["id"] == "download" for x in job["steps"]) else "audio"
    try:
        proc = subprocess.Popen(command, cwd=folder, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        with JOBS_LOCK:
            JOBS[job_id]["process"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            with JOBS_LOCK:
                current = JOBS[job_id]
                current["logs"].append(line)
                current["logs"] = current["logs"][-MAX_LOG_LINES:]
                if "Downloading video" in line:
                    current["stage"] = "Downloading source video"
                    current["stage_code"] = "download"
                elif "Extracting audio" in line or "Analysing loudness" in line:
                    current["stage"] = "Analyzing audio"
                    current["stage_code"] = "audio"
                elif "Using the saved loudness" in line:
                    current["stage"] = "Loading saved audio analysis"
                    current["stage_code"] = "audio"
                elif "Scanning whole video" in line:
                    current["stage"] = "Scanning visual changes"
                    current["stage_code"] = "visual"
                elif "Transcribing" in line:
                    current["stage"] = "Transcribing speech"
                    current["stage_code"] = "transcript"
                elif "Found " in line and "moments" in line:
                    current["stage"] = "Rendering clips"
                    current["stage_code"] = "render"
        code = proc.wait()
        if code:
            with JOBS_LOCK:
                JOBS[job_id].update(status="failed", stage="Could not generate clips",
                                    error="The clipper exited with an error. See the log below.")
            return
        result = json.loads((folder / "clips" / "clips.json").read_text())
        with JOBS_LOCK:
            JOBS[job_id].update(status="done", stage="Ready", stage_code="done", clips=result)
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", stage="Could not generate clips",
                                error=str(exc))


class Handler(BaseHTTPRequestHandler):
    server_version = "StreamToShorts/1.0"

    def log_message(self, fmt, *args):
        print("[web] " + fmt % args, flush=True)

    def send_json(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.serve_static("index.html")
        if path in {"/style.css", "/app.js"}:
            return self.serve_static(path.lstrip("/"))
        if path.startswith("/static/"):
            return self.serve_static(path.removeprefix("/static/"))
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job:
                    job = {k: v for k, v in job.items() if k not in {"process", "folder"}}
            return self.send_json(200 if job else 404, job or {"error": "Job not found"})
        if path.startswith("/media/"):
            return self.serve_media(path.removeprefix("/media/"))
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/jobs/link":
            try:
                payload = self.read_json()
                source = public_video_url(str(payload.get("url", "")).strip())
                options = read_options(payload)
                job_id = create_job(source, options)
                return self.send_json(202, {"id": job_id})
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_json(400, {"error": str(exc)})
        if path == "/api/jobs/upload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                name = Path(unquote(self.headers.get("X-Filename", "video.mp4"))).name
                if not name.lower().endswith(".mp4"):
                    raise ValueError("Please choose an MP4 file.")
                if length <= 0 or length > MAX_UPLOAD_BYTES:
                    raise ValueError(f"Upload must be between 1 byte and {MAX_UPLOAD_BYTES // 1024**3} GB.")
                if shutil.disk_usage(JOBS_ROOT if JOBS_ROOT.exists() else JOBS_ROOT.parent).free < length + 2 * 1024**3:
                    raise ValueError("There is not enough free disk space for this video and its clips.")
                options = read_options(parse_qs(urlparse(self.path).query))
                job_id = uuid.uuid4().hex[:12]
                folder = JOBS_ROOT / job_id
                folder.mkdir(parents=True, exist_ok=False)
                source = folder / name
                remaining = length
                with source.open("wb") as target:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Upload ended before the full video arrived.")
                        target.write(chunk)
                        remaining -= len(chunk)
                (folder / "clips").mkdir()
                with JOBS_LOCK:
                    JOBS[job_id] = make_job_record(job_id, folder, options, source_is_url=False)
                command = [sys.executable, str(ROOT / "clipper.py"), str(source),
                           "--count", str(options["count"]), "--length", str(options["length"]),
                           "--min-length", str(options["min_length"]), "--reframe", options["reframe"],
                           "--out", str(folder / "clips")]
                if options.get("visual_scan"):
                    command.append("--visual-scan")
                if options.get("no_transcribe"):
                    command.append("--no-transcribe")
                threading.Thread(target=run_job, args=(job_id, command, folder), daemon=True).start()
                return self.send_json(202, {"id": job_id})
            except (ValueError, OSError) as exc:
                return self.send_json(400, {"error": str(exc)})
        self.send_error(404)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def serve_static(self, relative):
        target = (WEB_ROOT / relative).resolve()
        if not target.is_relative_to(WEB_ROOT.resolve()) or not target.is_file():
            return self.send_error(404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_media(self, relative):
        parts = relative.split("/", 1)
        if len(parts) != 2 or not re.fullmatch(r"[a-f0-9]{12}", parts[0]):
            return self.send_error(404)
        job_id, filename = parts
        if Path(filename).name != filename or not filename.lower().endswith(".mp4"):
            return self.send_error(404)
        target = (JOBS_ROOT / job_id / "clips" / filename).resolve()
        if not target.is_relative_to((JOBS_ROOT / job_id / "clips").resolve()) or not target.is_file():
            return self.send_error(404)
        size = target.stat().st_size
        range_header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        start, end = 0, size - 1
        if match:
            start = int(match.group(1) or 0)
            end = min(int(match.group(2)) if match.group(2) else end, end)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        status = 206 if match else 200
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if match:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with target.open("rb") as media:
            media.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = media.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def read_options(payload):
    def get(name, default):
        value = payload.get(name, default) if isinstance(payload, dict) else payload.get(name, [default])
        if isinstance(value, list):
            return value[-1] if value else default
        return value
    try:
        count = int(get("count", 5))
    except (TypeError, ValueError):
        raise ValueError("Clip count must be between 1 and 20.") from None
    try:
        length = int(get("length", 30))
        min_length = int(get("min_length", 15))
    except (TypeError, ValueError):
        raise ValueError("Clip duration must be 5–180 seconds, with minimum no greater than maximum.") from None
    reframe = str(get("reframe", "fit"))
    if not 1 <= count <= 20:
        raise ValueError("Clip count must be between 1 and 20.")
    if not 5 <= min_length <= length <= 180:
        raise ValueError("Clip duration must be 5–180 seconds, with minimum no greater than maximum.")
    if reframe not in {"fit", "crop", "square"}:
        raise ValueError("Choose fit, crop, or square framing.")
    return {"count": count, "length": length, "min_length": min_length, "reframe": reframe,
            "visual_scan": str(get("visual_scan", "true")).lower() in {"true", "1", "on"},
            "no_transcribe": str(get("no_transcribe", "false")).lower() in {"true", "1", "on"}}


def main():
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("STREAM_TO_SHORTS_HOST", "127.0.0.1")
    port = int(os.environ.get("STREAM_TO_SHORTS_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Stream to Shorts UI: http://{host}:{port}", flush=True)
    print("Local-only by default. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
