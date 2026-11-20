#!/usr/bin/env python3
"""Serve a local candidate review page and save an approval manifest."""
import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import clipper


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stream to Shorts · Candidate review</title>
<style>
:root{color-scheme:dark;font:16px system-ui;background:#101216;color:#f4f5f7}
body{max-width:1050px;margin:28px auto;padding:0 16px}h1{margin-bottom:5px}
p{color:#b5bdc9}.layout{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(270px,.8fr);gap:22px}
video{width:100%;max-height:68vh;background:#000;border-radius:12px;position:sticky;top:12px}
.candidate{background:#1c2028;border:1px solid #343a46;border-radius:12px;padding:14px;margin:0 0 12px}
.candidate h3{margin:0 0 8px}.meta{font-size:14px;color:#bec5d0;line-height:1.6}
button{border:0;border-radius:8px;padding:10px 13px;color:#fff;background:#5b55e7;font:inherit;cursor:pointer}
button.secondary{background:#343a46}.controls{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
label{display:flex;align-items:center;gap:8px;margin:12px 0 0}input{accent-color:#766eff;width:18px;height:18px}
#status{min-height:24px;color:#a9e5ba}.count{font-variant-numeric:tabular-nums}
@media(max-width:750px){.layout{grid-template-columns:1fr}video{position:static}}
</style>
</head>
<body>
<h1>Review clip candidates</h1>
<p>Play each source-video range, approve the moments you want, then save the selection for export.</p>
<div class="layout"><section><video id="player" controls preload="metadata"></video></section>
<section><div class="controls"><button id="save">Save approved candidates</button>
<button class="secondary" id="all">Approve all</button>
<button class="secondary" id="none">Reject all</button></div>
<div id="status" role="status"></div><div id="list"></div></section></div>
<script>
const player=document.querySelector('#player'),list=document.querySelector('#list'),status=document.querySelector('#status');
let candidates=[];let activeEnd=0;
player.src='/media';
player.addEventListener('timeupdate',()=>{if(activeEnd&&player.currentTime>=activeEnd){player.pause();activeEnd=0}});
const stamp=n=>{n=Math.floor(Number(n)||0);return `${Math.floor(n/3600)}:${String(Math.floor(n%3600/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`};
function card(candidate,index){
 const el=document.createElement('article');el.className='candidate';
 const heading=document.createElement('h3');heading.textContent=`Candidate ${index+1}`;el.append(heading);
 const meta=document.createElement('div');meta.className='meta';
 meta.textContent=`${stamp(candidate.start)}–${stamp(candidate.end)} · peak ${stamp(candidate.peak)} · score ${candidate.rank??candidate.score??'—'} · ${Array.isArray(candidate.sources)?candidate.sources.join(', '):'detector'}`;el.append(meta);
 if(candidate.rubric_explanation){const note=document.createElement('p');note.textContent=candidate.rubric_explanation;el.append(note)}
 const play=document.createElement('button');play.className='secondary';play.textContent='Play this range';
 play.addEventListener('click',async()=>{activeEnd=Number(candidate.end);if(player.currentTime<Number(candidate.start)||player.currentTime>=activeEnd)player.currentTime=Number(candidate.start);try{await player.play()}catch(e){status.textContent='Playback could not start. Press play on the video.'}});el.append(play);
 const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.dataset.index=index;
 label.append(check,document.createTextNode('Approve this candidate'));el.append(label);return el;
}
fetch('/candidates').then(r=>r.json()).then(data=>{candidates=data;data.forEach((item,index)=>list.append(card(item,index)));status.textContent=`${data.length} candidates loaded.`}).catch(()=>{status.textContent='Could not load candidate data.'});
document.querySelector('#all').onclick=()=>document.querySelectorAll('input[type=checkbox]').forEach(x=>x.checked=true);
document.querySelector('#none').onclick=()=>document.querySelectorAll('input[type=checkbox]').forEach(x=>x.checked=false);
document.querySelector('#save').onclick=async()=>{const approved_indices=[...document.querySelectorAll('input[type=checkbox]:checked')].map(x=>Number(x.dataset.index));
 if(!approved_indices.length){status.textContent='Approve at least one candidate before saving.';return}
 const response=await fetch('/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approved_indices})});
 const result=await response.json();status.textContent=response.ok?`Saved ${result.count} approved candidates to ${result.output}.`:result.error};
</script>
</body></html>"""


def build_approval_manifest(candidates, approved_indices, video):
    approved = []
    seen = set()
    for index in approved_indices:
        if type(index) is not int or index < 0 or index >= len(candidates):
            raise ValueError(f"invalid candidate index: {index!r}")
        if index not in seen:
            seen.add(index)
            approved.append(candidates[index])
    if not approved:
        raise ValueError("approve at least one candidate")
    return {
        "source": Path(video).name,
        "source_sha256": clipper.video_sha256(video),
        "candidates": approved,
    }


def save_approved_candidates(candidates, approved_indices, video, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_approval_manifest(candidates, approved_indices, video)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temp.replace(output)
    return manifest


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler_for(video, candidates, output):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"preview: {fmt % args}")

        def send_json(self, status, data):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/candidates":
                self.send_json(200, candidates)
            elif self.path == "/media":
                self.send_video()
            else:
                self.send_error(404)

        def send_video(self):
            size = video.stat().st_size
            start, end = 0, size - 1
            byte_range = self.headers.get("Range")
            if byte_range:
                try:
                    unit, bounds = byte_range.split("=", 1)
                    if unit != "bytes":
                        raise ValueError
                    left, right = bounds.split("-", 1)
                    if left:
                        start = int(left)
                    else:
                        start = max(0, size - int(right))
                    if right and left:
                        end = min(end, int(right))
                    if start > end or start >= size:
                        raise ValueError
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            length = end - start + 1
            self.send_response(206 if byte_range else 200)
            self.send_header("Content-Type", mimetypes.guess_type(video.name)[0] or "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with video.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def do_POST(self):
            if self.path != "/approve":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size))
                manifest = save_approved_candidates(
                    candidates, payload.get("approved_indices", []), video, output
                )
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            self.send_json(200, {"count": len(manifest["candidates"]), "output": str(output)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="local source video to preview")
    parser.add_argument("candidates", type=Path, help="clips/candidates.json from clipper.py")
    parser.add_argument("--output", type=Path, default=Path("clips/approved.json"),
                        help="approval manifest path (default: clips/approved.json)")
    parser.add_argument("--port", type=int, default=8765,
                        help="localhost port (default 8765; use 0 for an available port)")
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error(f"video file not found: {args.video}")
    try:
        data = json.loads(args.candidates.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"could not read candidates JSON: {exc}")
    if not isinstance(data, list) or not data:
        parser.error("candidate JSON must be a nonempty array")
    if any(not isinstance(item, dict) or item.get("source") != args.video.name
           for item in data):
        parser.error("candidate JSON source does not match this video filename")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    server = PreviewServer(("127.0.0.1", args.port), handler_for(
        args.video.resolve(), data, args.output.resolve()
    ))
    host, port = server.server_address
    print(f"Local review page: http://{host}:{port}/")
    print(f"Approved selections will be saved to {args.output.resolve()}")
    print("Press Ctrl+C to stop the preview server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping preview server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
