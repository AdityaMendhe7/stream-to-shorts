#!/usr/bin/env python3
"""
Live mode for the Json clipper.

Watches a LIVE stream, transcribes it in real time, flags hype moments as they happen
(loudness + speech + live chat), records the stream, and cuts the best clips afterwards.

    python live.py "https://www.twitch.tv/streamer"                 # watch + transcribe + flag
    python live.py "https://www.youtube.com/watch?v=XXXX" --auto-clip --count 5
    python live.py --cut live/20261120_010000 --count 5             # cut clips from a session
    python live.py some_video.mp4 --speed 10                        # simulate a stream from a file

Press Ctrl+C to stop. Everything is saved in live/<date_time>/ :
    recording.ts  transcript.txt  transcript.json  moments.json  clips/
"""
import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

import clipper

SR = 16000
BPS = SR * 2          # bytes per second of 16-bit mono audio
STEP = 8              # transcribe every N seconds of new audio
OVERLAP = 2.0         # re-listen to the last N seconds so words aren't cut in half
JUNK = {"thank you.", "thank you", "thanks for watching!", "thanks for watching.",
        "thanks for watching", "you", "bye.", "bye"}  # classic Whisper silence hallucinations


def hms(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------- live state: signals, transcript, moments ----------
class Live:
    def __init__(self, args, sdir):
        self.args, self.sdir = args, Path(sdir)
        self.lang = None if args.lang == "auto" else args.lang
        self.delay = 6 if args.no_transcribe else 14   # wait for the transcript to catch up
        self.t = 0                       # seconds of audio processed
        self.rms_hist, self.loud = [], []
        self.speech = defaultdict(float)
        self.chat_total = defaultdict(int)
        self.chat_hype = defaultdict(int)
        self.segments, self.moments = [], []
        self.committed, self.last_tok = 0.0, None
        self.next_check = 0
        self.txt = open(self.sdir / "transcript.txt", "w", encoding="utf-8")

    # --- audio loudness (robust z-score against the last 10 minutes) ---
    def add_audio_second(self, chunk):
        x = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        h = self.rms_hist[-600:]
        z = 0.0
        if len(h) >= 20:
            med = float(np.median(h))
            mad = float(np.median(np.abs(np.array(h) - med))) * 1.4826
            mad = max(mad, 0.05 * med, 1.0)
            prev = float(np.median(h[-30:]))   # reward sudden jumps, not constant loudness
            z = 0.4 * (rms - med) / mad + 0.6 * (rms - prev) / mad
            z = min(10.0, max(0.0, z))
        self.rms_hist.append(rms)
        self.loud.append(z)
        self.t += 1

    # --- chat ---
    def on_chat(self, text):
        s = int(max(0, self.t - self.args.chat_delay))
        self.chat_total[s] += 1
        if clipper.is_hype_message(text):
            self.chat_hype[s] += 1

    # --- transcript ---
    def transcribe_window(self, model, audio_bytes, win_start, final=False):
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        win_end = win_start + len(audio) / SR
        segs, _ = model.transcribe(audio, language=self.lang, word_timestamps=True,
                                   vad_filter=True, beam_size=1,
                                   condition_on_previous_text=False)
        self.commit_window(win_start, win_end, list(segs), final)

    def commit_window(self, win_start, win_end, segs, final=False):
        """Keep only NEW words; skip ones cut off at the window edge (the next window,
        which overlaps, will transcribe them properly)."""
        for seg in segs:
            words = []
            for w in (seg.words or []):
                st, en = win_start + w.start, win_start + w.end
                tok = w.word.strip()
                if not tok or st < self.committed - 0.05:
                    continue
                if not final and en > win_end - 0.4:
                    continue
                if self.last_tok and tok.lower() == self.last_tok[0] and st - self.last_tok[1] < 0.6:
                    continue
                words.append({"start": round(st, 2), "end": round(en, 2), "word": tok})
                self.last_tok = (tok.lower(), st)
            if not words:
                continue
            text = " ".join(x["word"] for x in words).strip()
            if text.lower() in JUNK:
                continue
            self.committed = max(self.committed, words[-1]["end"])
            self.segments.append({"start": words[0]["start"], "end": words[-1]["end"],
                                  "text": text, "words": words})
            for sec, v in clipper.line_scores(words).items():
                self.speech[sec] += v
            line = f"[{hms(words[0]['start'])}] {text}"
            print(line, flush=True)
            self.txt.write(line + "\n")
            self.txt.flush()

    def worker(self, model, jobs):
        while True:
            job = jobs.get()
            if job is None:
                return
            try:
                self.transcribe_window(model, *job)
            except Exception as e:  # keep the stream running whatever happens
                print(f"  (transcription error: {e})", flush=True)

    # --- moment detection ---
    def chat_base(self, s):
        arr = np.array([self.chat_total.get(i, 0) for i in range(max(0, s - 300), s + 1)],
                       dtype=np.float32)
        if len(arr) < 30:
            return None
        return float(arr.mean()), max(float(arr.std()), 0.5)

    def comp(self, i, base):
        loud = self.loud[i] if 0 <= i < len(self.loud) else 0.0
        speech = self.speech.get(i, 0.0) if i >= 0 else 0.0
        chat = 0.0
        if base is not None and i >= 0:
            x = (self.chat_total.get(i - 1, 0) + self.chat_total.get(i, 0)
                 + self.chat_total.get(i + 1, 0)) / 3
            z = min(10.0, max(0.0, (x - base[0]) / base[1]))
            chat = 0.6 * z + 2.0 * float(np.log1p(self.chat_hype.get(i, 0)))
        return loud, speech, chat

    def smooth(self, s):
        base = self.chat_base(s)
        tot = np.zeros(3)
        for i in range(s - 2, s + 3):
            tot += self.comp(i, base)
        return tot / 5

    def snippet(self, s):
        words = []
        for seg in reversed(self.segments):
            if seg["end"] < s - 6:
                break
            if seg["start"] <= s + 3:
                words = [w["word"] for w in seg["words"] if s - 6 <= w["start"] <= s + 3] + words
        return " ".join(words)

    def check_moment(self, final=False):
        horizon = self.t - 1 - (4 if final else self.delay)
        while self.next_check <= horizon:
            self.check_at(self.next_check)
            self.next_check += 1

    def check_at(self, s):
        if s < 20:
            return
        sm = self.smooth(s)
        sc = float(sm.sum())
        if sc < self.args.threshold:
            return
        for k in range(-4, 5):
            if k == 0:
                continue
            o = float(self.smooth(s + k).sum())
            if o > sc or (k < 0 and o == sc):
                return  # not the top of its local peak
        signals = int(sm[0] >= 1.5) + int(sm[1] >= 0.3) + int(sm[2] >= 1.0)
        m = {"peak": s, "score": round(sc, 2), "loud": round(float(sm[0]), 2),
             "speech": round(float(sm[1]), 2), "chat": round(float(sm[2]), 2),
             "signals": signals,
             "rank": round(sc * (1 + 0.3 * max(0, signals - 1)), 2),
             "text": self.snippet(s)}
        if self.moments and s - self.moments[-1]["peak"] < self.args.cooldown:
            if m["rank"] > self.moments[-1]["rank"]:   # same burst: keep the strongest peak
                self.moments[-1].update(m)
                self.save()
            return
        self.moments.append(m)
        self.save()
        print(f"\n🔥 MOMENT {hms(s)}  score {m['score']}  "
              f"[loud {m['loud']} | speech {m['speech']} | chat {m['chat']}]", flush=True)
        if m["text"]:
            print(f'   "{m["text"]}"\n', flush=True)

    def score_array(self):
        """Combined per-second excitement, used later to trim clips to the action."""
        n = self.t
        if n == 0:
            return np.zeros(0)
        loud = np.array(self.loud[:n], dtype=float)
        speech = np.array([self.speech.get(i, 0.0) for i in range(n)])
        tot = np.array([self.chat_total.get(i, 0) for i in range(n + 1)], dtype=float)
        hype = np.array([self.chat_hype.get(i, 0) for i in range(n)])
        sm3 = np.convolve(tot, np.ones(3) / 3, mode="same")[:n]
        z = np.clip((sm3 - sm3.mean()) / max(float(sm3.std()), 0.5), 0, 10)
        return loud + speech + 0.6 * z + 2.0 * np.log1p(hype)

    def save(self):
        (self.sdir / "moments.json").write_text(json.dumps(self.moments, indent=2))
        (self.sdir / "transcript.json").write_text(json.dumps(self.segments))
        (self.sdir / "score.json").write_text(
            json.dumps([round(float(x), 2) for x in self.score_array()]))
        (self.sdir / "loud.json").write_text(
            json.dumps([round(float(x), 2) for x in self.loud]))


# ---------- chat sources ----------
def twitch_chat(channel, on_msg, stop, host="irc.chat.twitch.tv", port=6667):
    """Read a Twitch channel's chat anonymously over IRC (no login, no extra packages)."""
    while not stop.is_set():
        try:
            s = socket.create_connection((host, port), timeout=15)
            s.settimeout(3)
            s.sendall(f"NICK justinfan{np.random.randint(10000, 99999)}\r\n"
                      f"JOIN #{channel}\r\n".encode())
            buf = ""
            while not stop.is_set():
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data.decode("utf-8", "ignore")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if line.startswith("PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG " in line and " :" in line.split(" PRIVMSG ", 1)[1]:
                        on_msg(line.split(" PRIVMSG ", 1)[1].split(" :", 1)[1])
            s.close()
        except OSError:
            pass
        stop.wait(5)  # reconnect after a short pause


def youtube_chat(url, on_msg, stop):
    try:
        from chat_downloader import ChatDownloader
    except ImportError:
        print("  (YouTube chat needs: pip install chat-downloader)")
        return
    try:
        for m in ChatDownloader().get_chat(url):
            if stop.is_set():
                break
            on_msg(m.get("message") or "")
    except Exception as e:
        print(f"  (YouTube chat stopped: {e})")


def start_chat(src, live, stop, args):
    if args.no_chat or Path(src).exists():
        return
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", src)
    if m and m.group(1).lower() not in ("videos", "directory"):
        threading.Thread(target=twitch_chat, args=(m.group(1).lower(), live.on_chat, stop),
                         daemon=True).start()
        print(f"Reading Twitch chat for #{m.group(1).lower()}")
    elif "youtu" in src:
        threading.Thread(target=youtube_chat, args=(src, live.on_chat, stop),
                         daemon=True).start()
        print("Reading YouTube live chat")
    else:
        print("No chat source for this link; using audio and speech only.")


# ---------- stream input ----------
def resolve_source(src, args):
    """Return ffmpeg input arguments for a live link or a local file."""
    p = Path(src)
    if p.exists():
        rate = ["-re"] if args.speed == 1 else ["-readrate", str(args.speed)]
        return rate + ["-i", str(p.resolve())]
    try:
        import yt_dlp
    except ImportError:
        sys.exit("yt-dlp missing. Run: pip install -r requirements.txt")
    opts = {"quiet": True, "noplaylist": True, "format": "best[height<=1080]/best"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(src, download=False)
    if not (info.get("is_live") or info.get("live_status") == "is_live"):
        sys.exit(f"This isn't live right now (status: {info.get('live_status') or 'not live'}). "
                 f"For a finished video use: python clipper.py \"{src}\"")
    url = info.get("url") or (info.get("formats") or [{}])[-1].get("url")
    if not url:
        sys.exit("Couldn't find a playable stream address for this link.")
    inp = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    ua = (info.get("http_headers") or {}).get("User-Agent")
    if ua:
        inp += ["-user_agent", ua]
    return inp + ["-i", url]


def load_model(size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper missing. Run: pip install -r requirements.txt "
                 "(or use --no-transcribe)")
    return WhisperModel(size, compute_type="int8")


def submit(jobs, job):
    """Queue a transcription job; if Whisper can't keep up, drop the oldest one."""
    while True:
        try:
            jobs.put_nowait(job)
            return
        except queue.Full:
            try:
                jobs.get_nowait()
                print("  (transcription is falling behind - try --model tiny)", flush=True)
            except queue.Empty:
                pass


# ---------- main live loop ----------
def run_live(a):
    sdir = Path(a.out) / datetime.now().strftime("%Y%m%d_%H%M%S")
    sdir.mkdir(parents=True, exist_ok=True)
    inp = resolve_source(a.source, a)
    live = Live(a, sdir)

    model, jobs, wt = None, queue.Queue(maxsize=2), None
    if not a.no_transcribe:
        print("Loading Whisper model (the first run downloads it)...", flush=True)
        model = load_model(a.model)
        wt = threading.Thread(target=live.worker, args=(model, jobs), daemon=True)
        wt.start()

    stop = threading.Event()
    start_chat(a.source, live, stop, a)

    rec = sdir / "recording.ts"
    log = open(sdir / "ffmpeg.log", "w")
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error"] + inp
           + ["-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "mpegts", str(rec),
              "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                            start_new_session=(os.name != "nt"))
    print(f"Listening. Saving to {sdir}/   (Ctrl+C to stop)\n", flush=True)

    buf, buf_t0, last_end = bytearray(), 0.0, 0.0
    try:
        while True:
            chunk = proc.stdout.read(BPS)
            if not chunk:
                break
            live.add_audio_second(chunk)
            buf.extend(chunk)
            if model and live.t - last_end >= STEP:
                start = max(buf_t0, last_end - OVERLAP)
                i0 = int((start - buf_t0) * SR) * 2
                submit(jobs, (bytes(buf[i0:]), start, False))
                last_end = float(live.t)
                cut = int((last_end - OVERLAP - buf_t0) * SR) * 2
                if cut > 0:
                    del buf[:cut]
                    buf_t0 += cut / BPS
            live.check_moment()
            if live.t % 30 == 0:
                live.save()
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        stop.set()
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        try:
            if model:
                print("Finishing transcription...", flush=True)
                if len(buf) > BPS // 2:
                    start = max(buf_t0, last_end - OVERLAP)
                    i0 = int((start - buf_t0) * SR) * 2
                    jobs.put((bytes(buf[i0:]), start, True))
                jobs.put(None)
                wt.join(timeout=300)
            live.check_moment(final=True)
        except KeyboardInterrupt:
            pass
        live.save()
        live.txt.close()

    summary(live, a, sdir)
    if a.auto_clip and live.moments:
        cut_session(sdir, a)


def summary(live, a, sdir):
    ranked = sorted(live.moments, key=lambda m: -m["rank"])
    sm = np.convolve(live.score_array(), np.ones(5) / 5, mode="same") if live.t else None
    print(f"\n=== Session over: {hms(live.t)} recorded, {len(live.moments)} moments ===")
    for i, m in enumerate(ranked[: a.count], 1):
        text = live.snippet(m["peak"]) or m["text"]
        print(f" #{i}  {hms(m['peak'])}  rank {m['rank']}  "
              f"(loud {m['loud']}, speech {m['speech']}, chat {m['chat']})")
        if sm is not None:
            c = clipper.plan_clip(m["peak"], sm, float(live.t), live.segments,
                                  a.length, a.min_length, a.lead)
            print(f"      clip window {hms(c['start'])} - {hms(c['end'])}   \"{text}\"")
    if live.moments and not a.auto_clip:
        print(f"\nCut the best clips with:\n  python live.py --cut {sdir} --count {a.count}")


# ---------- cutting clips from a saved session ----------
def cut_session(sdir, a):
    sdir = Path(sdir)
    mfile, rec = sdir / "moments.json", sdir / "recording.ts"
    if not mfile.exists() or not rec.exists():
        sys.exit(f"{sdir} doesn't look like a live session (needs moments.json + recording.ts)")
    moments = json.loads(mfile.read_text())
    tfile, sfile = sdir / "transcript.json", sdir / "score.json"
    segments = json.loads(tfile.read_text()) if tfile.exists() else []
    sm = None
    if sfile.exists():
        sc = np.array(json.loads(sfile.read_text()), dtype=float)
        if len(sc):
            sm = np.convolve(sc, np.ones(5) / 5, mode="same")
    total = clipper.duration(rec)
    cam = clipper.parse_cam(a.cam) if a.cam else None
    if a.reframe == "stack" and not cam:
        sys.exit("The stack layout needs --cam x:y:w:h (e.g. --cam 0.72:0.62:0.28:0.38).")
    a.min_length = min(a.min_length, a.length)

    pool = list(moments)
    if getattr(a, "visual_scan", False):
        print(f"Scanning saved live recording for visual-change candidates "
              f"({a.visual_fps:g} frames/sec)...", flush=True)
        try:
            visual_raw = clipper.visual_change_scores(rec, int(total), a.visual_fps)
            loud_file = sdir / "loud.json"
            audio_scores = (np.asarray(json.loads(loud_file.read_text()), dtype=np.float32)
                            if loud_file.exists()
                            else clipper.video_audio_scores(rec, int(total)))
            visual = clipper.combine_visual_audio_scores(visual_raw, audio_scores)
            vm, _ = clipper.pick_moments({"visual": visual}, max(a.count * 3, a.count),
                                         a.length, 1.0)
            for candidate in vm:
                if all(abs(candidate["peak"] - m["peak"]) >= max(5, a.length // 2)
                       for m in pool):
                    candidate["sources"] = ["visual"]
                    candidate["visual_only"] = True
                    candidate["visual_raw"] = round(float(visual_raw[candidate["peak"]]), 2)
                    candidate["visual"] = round(float(visual[candidate["peak"]]), 2)
                    pool.append(candidate)
        except Exception as e:
            print(f"  Visual scan unavailable; using audio/chat moments: {e}")

    # Build a wider pool when Gemini is enabled; it ranks but cannot invent or
    # alter timestamps. Existing live candidates remain eligible independently.
    pool_limit = max(a.count, min(50, a.count * 3 if a.gemini_review else a.count))
    chosen = []
    for m in sorted(pool, key=lambda m: -m["rank"]):
        if all(abs(m["peak"] - c["peak"]) >= a.length for c in chosen):
            chosen.append(m)
        if len(chosen) >= pool_limit:
            break
    if not chosen:
        sys.exit("No moments to cut.")

    if a.gemini_review:
        for m in chosen:
            start = max(0.0, m["peak"] - a.lead)
            fallback = {"start": start, "end": min(total, start + a.length)}
            window = (clipper.plan_clip(m["peak"], sm, total, segments,
                                        a.length, a.min_length, a.lead)
                      if sm is not None and not m.get("visual_only") else fallback)
            m.update(window)
        chosen = clipper.gemini_review(
            rec, chosen, segments, sdir, a.gemini_model,
            clipper.parse_rubric_weights(a.rubric_weights))
    chosen = chosen[:a.count]

    out = sdir / "clips"
    out.mkdir(exist_ok=True)
    print(f"Cutting {len(chosen)} clips (best first)...")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        brand = None
        if not a.no_brand:
            brand = clipper.make_brand(a.handle, tmp / "brand.png", a.reframe)
        for i, m in enumerate(chosen, 1):
            if sm is not None and m["peak"] < len(sm) and not m.get("visual_only"):
                clip = clipper.plan_clip(m["peak"], sm, total, segments,
                                         a.length, a.min_length, a.lead)
            else:  # older session without score.json: fixed window
                start = max(0.0, m["peak"] - a.lead)
                clip = {"start": start, "end": min(total, start + a.length)}
            caps = (clipper.make_captions(segments, clip["start"], clip["end"],
                                          tmp / f"caps_{i}", a.reframe)
                    if segments and not a.no_captions else None)
            name = out / f"clip_{i:02d}_{hms(m['peak']).replace(':', '-')}.mp4"
            clipper.render(rec, clip, name, a.reframe, caps, tmp, brand, cam, a.crop_x)
            print(f"  {name.name}  {clip['end'] - clip['start']:.0f}s  (rank {m['rank']})")
    print(f"Done. Clips are in {out}/  (clip_01 is the best)")


def main():
    p = argparse.ArgumentParser(description="Live stream transcription + hype-moment finder.")
    p.add_argument("source", nargs="?", help="live stream link (Twitch/YouTube/Kick) or a video file")
    p.add_argument("--cut", metavar="SESSION_DIR", help="cut clips from a saved live session")
    p.add_argument("--auto-clip", action="store_true", help="cut clips automatically when done")
    p.add_argument("--count", type=int, default=8, help="clips to cut / show (default 8)")
    p.add_argument("--length", type=int, default=30, help="longest a clip can be (default 30)")
    p.add_argument("--min-length", type=int, default=15, help="shortest a clip can be (default 15)")
    p.add_argument("--lead", type=int, default=5,
                   help="seconds of set-up before the action starts (default 5)")
    p.add_argument("--reframe", choices=clipper.LAYOUTS, default="fit",
                   help="fit = whole frame, square = bigger centre crop, "
                        "stack = gameplay + facecam (needs --cam), crop = fill screen")
    p.add_argument("--cam", help="facecam area as x:y:w:h fractions of the frame (for stack)")
    p.add_argument("--crop-x", type=float, default=0.5,
                   help="horizontal crop anchor from 0 (left) to 1 (right), for --reframe crop")
    p.add_argument("--visual-scan", action="store_true",
                   help="independently scan a saved recording for visual-change candidates")
    p.add_argument("--visual-fps", type=float, default=0.5,
                   help="visual scan sampling rate in frames/sec (default 0.5; max 2)")
    p.add_argument("--gemini-review", action="store_true",
                   help="rank cut candidates with Gemini using transcript excerpts and sampled frames")
    p.add_argument("--gemini-model", default="gemini-3.8-flash",
                   help="Gemini model for --gemini-review")
    p.add_argument("--rubric-weights",
                   help='JSON weight overrides, e.g. {"hook":0.25,"intensity":0.25}')
    p.add_argument("--handle", default="@gtasixviralclips",
                   help="Instagram/channel handle shown at the bottom")
    p.add_argument("--no-brand", action="store_true", help="don't show the bottom handle")
    p.add_argument("--model", default="base", help="whisper model: tiny/base/small")
    p.add_argument("--lang", default="en", help="speech language code, or 'auto'")
    p.add_argument("--no-transcribe", action="store_true", help="skip live transcription")
    p.add_argument("--no-captions", action="store_true",
                   help="skip burned-in captions when cutting a saved session")
    p.add_argument("--no-chat", action="store_true", help="don't read viewer chat")
    p.add_argument("--chat-delay", type=float, default=3.0,
                   help="seconds viewers take to react (default 3)")
    p.add_argument("--threshold", type=float, default=2.5, help="raise for fewer moments")
    p.add_argument("--cooldown", type=int, default=40, help="min seconds between moments")
    p.add_argument("--speed", type=float, default=1.0,
                   help="for local test files only: playback speed (default 1 = real time)")
    p.add_argument("--out", default="live", help="folder for session data (default live)")
    a = p.parse_args()
    if not 0 <= a.crop_x <= 1:
        p.error("--crop-x must be between 0 (left) and 1 (right); use --reframe fit to keep the full source visible")
    if not 0 < a.visual_fps <= 2:
        p.error("--visual-fps must be greater than 0 and at most 2")
    if a.count < 1 or a.length < 1 or a.min_length < 1 or a.lead < 0:
        p.error("--count, --length, and --min-length must be positive; --lead cannot be negative")
    cam = clipper.parse_cam(a.cam) if a.cam else None
    if a.reframe == "stack" and (a.cut or a.auto_clip) and not cam:
        p.error("the stack layout needs --cam x:y:w:h with a positive rectangle inside the frame")
    a.rubric_weights = clipper.parse_rubric_weights(a.rubric_weights)
    if a.gemini_review:
        a.visual_scan = True

    clipper.need("ffmpeg")
    clipper.need("ffprobe")
    if a.cut:
        cut_session(a.cut, a)
        return
    if not a.source:
        p.error("give a live stream link, or --cut SESSION_DIR")
    run_live(a)


if __name__ == "__main__":
    main()
