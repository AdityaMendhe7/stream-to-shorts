#!/usr/bin/env python3
"""
Json clipper: finds the hype moments in a long gameplay/stream video
and cuts them into polished vertical (9:16) clips with highlighted captions.

Detection = loudness spikes + hype words from a local Whisper transcript +
viewer chat activity (message speed and hype words like LMAO / WTF) when available. Everything runs locally and is free.

Usage:
    python clipper.py my_stream.mp4 --count 8 --length 30
    python clipper.py "https://www.twitch.tv/videos/123456" --count 8
    python clipper.py my_stream.mp4 --no-transcribe        # audio only, no captions
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import numpy as np

# Hype vocabulary. Strong reactions count a lot, everyday words that are only
# sometimes a reaction ("no", "what", "go") count a little, and whole phrases or
# repeated words ("no no no no") count the most.
STRONG_WORDS = {
    "wtf", "omg", "holy", "insane", "crazy", "dead", "died", "lmao", "lol", "gg", "unreal",
    "clutch", "boom", "bro", "dude", "damn", "wow", "nooo", "noooo", "ahh", "aah", "scream",
    "screaming", "cooked", "cracked", "sheesh", "bruh", "jesus",
}
WEAK_WORDS = {
    "no", "what", "oh", "yo", "wait", "go", "run", "help", "shoot", "shot", "kill", "killed",
    "crash", "crashed", "explode", "exploded", "cops", "police", "wanted", "escape", "jump",
    "fall", "fell", "chase", "nice", "lets", "let's", "clip", "dies", "stop", "ow",
}
HYPE_WORDS = STRONG_WORDS | WEAK_WORDS
PHRASES = [
    "what the fuck", "what the hell", "what the heck", "oh my god", "oh my gosh", "no way",
    "let's go", "lets go", "holy shit", "holy fuck", "are you kidding", "i'm dead", "im dead",
    "oh no", "what is happening", "hell no", "get out", "shut up", "bro what", "what was that",
    "he's cooked",
]
PHRASE_TOKENS = [tuple(re.sub(r"[^a-z' ]", "", ph).split()) for ph in PHRASES]


def word_weight(tok):
    if tok in STRONG_WORDS:
        return 2.0
    if tok in WEAK_WORDS:
        return 0.4
    return 0.0


def line_scores(words):
    """Hype points per second for a run of transcribed words ({'start','word'} dicts)."""
    out = defaultdict(float)
    toks = [re.sub(r"[^a-z']", "", w["word"].lower()) for w in words]
    for w, t in zip(words, toks):
        sec = int(w["start"])
        out[sec] += word_weight(t)
        if "!" in w["word"]:
            out[sec] += 0.5
    for ph in PHRASE_TOKENS:
        n = len(ph)
        for i in range(len(toks) - n + 1):
            if tuple(toks[i:i + n]) == ph:
                out[int(words[i]["start"])] += 3.0
    i = 0
    while i < len(toks):  # "no no no no" / "go go go" bursts
        j = i
        while toks[i] and j + 1 < len(toks) and toks[j + 1] == toks[i]:
            j += 1
        if j - i + 1 >= 3:
            out[int(words[i]["start"])] += 2.0
        i = j + 1
    return out


# ---------- helpers ----------
def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Command failed: {' '.join(map(str, cmd))}\n{r.stderr[-1500:]}")
    return r.stdout


def need(tool):
    try:
        subprocess.run([tool, "-version"], capture_output=True, check=True)
    except Exception:
        sys.exit(f"'{tool}' not found. Install ffmpeg (https://ffmpeg.org) and add it to PATH.")


def download(url, dest_dir, want_chat=True, max_height=1080):
    """Download a video from a link (Twitch VOD, YouTube, Kick, etc.) via yt-dlp.
    Returns (video_path, chat_path_or_None)."""
    try:
        import yt_dlp
    except ImportError:
        sys.exit("yt-dlp missing. Run: pip install -r requirements.txt")
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(dest_dir / "%(title).80s.%(ext)s"),
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "concurrent_fragment_downloads": 8,
        "merge_output_format": "mp4",
        "noplaylist": True,
    }
    is_yt = "youtu" in url
    if want_chat and is_yt:  # YouTube stores chat replay as a "live_chat" subtitle track
        opts.update({"writesubtitles": True, "subtitleslangs": ["live_chat"]})
    print("Downloading video...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        meta = ydl.extract_info(url, download=False)
        if meta.get("is_live"):
            sys.exit("This stream is still live. Run again after it ends, using the "
                     "replay/VOD link, so the full video and chat replay exist.")
        info = ydl.process_ie_result(meta, download=True)
        done = info.get("requested_downloads") or []
        path = Path(done[0]["filepath"] if done else ydl.prepare_filename(info))

    chat = None
    if want_chat:
        if is_yt:
            found = sorted(dest_dir.glob(f"{path.stem}*.live_chat.json"))
            chat = found[0] if found else None
        elif "twitch.tv/videos/" in url:
            vid = re.search(r"videos/(\d+)", url)
            if vid and shutil.which("TwitchDownloaderCLI"):
                chat = dest_dir / f"{vid.group(1)}_chat.json"
                print("Downloading Twitch chat...")
                r = subprocess.run(["TwitchDownloaderCLI", "chatdownload", "--id", vid.group(1),
                                    "-o", str(chat)], capture_output=True, text=True)
                if r.returncode != 0 or not chat.exists():
                    print("  Twitch chat download failed; continuing without chat.")
                    chat = None
            else:
                print("  Twitch chat needs TwitchDownloaderCLI "
                      "(github.com/lay295/TwitchDownloader). Export the chat as JSON and "
                      "pass it with --chat-file. Continuing without chat.")
    return path, chat


def duration(video):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(video)])
    return float(out.strip())


# ---------- detection ----------
def loudness_scores(wav_path, n_secs):
    """Per-second loudness score. Mixes 'loud for this video' with 'much louder than the
    previous 30 seconds', so a sudden scream beats constant loud music or gunfire."""
    rms = np.zeros(n_secs, dtype=np.float32)
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        for i in range(n_secs):  # one second at a time: low memory even for 9-hour videos
            chunk = np.frombuffer(w.readframes(sr), dtype=np.int16).astype(np.float32)
            if len(chunk) == 0:
                break
            rms[i] = np.sqrt(np.mean(chunk ** 2))
    med = float(np.median(rms))
    mad = max(float(np.median(np.abs(rms - med))) * 1.4826, 0.05 * med, 1.0)
    prev = np.full(n_secs, med, dtype=np.float32)
    for i in range(1, n_secs):
        prev[i] = np.median(rms[max(0, i - 30):i])
    z = 0.4 * (rms - med) / mad + 0.6 * (rms - prev) / mad
    return np.clip(z, 0, 10)


def load_whisper(size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper missing. Run: pip install -r requirements.txt "
                 "(or use --no-transcribe)")
    return WhisperModel(size, compute_type="int8")


def _run_whisper(model, audio, offset, lang):
    """Transcribe float32 samples; returns segments with times shifted by `offset` seconds."""
    segs, _ = model.transcribe(audio, language=lang, word_timestamps=True, vad_filter=True,
                               beam_size=1, condition_on_previous_text=False)
    out = []
    for s in segs:
        words = [{"start": round(w.start + offset, 2), "end": round(w.end + offset, 2),
                  "word": w.word.strip()} for w in (s.words or [])]
        if words:
            out.append({"start": round(s.start + offset, 2), "end": round(s.end + offset, 2),
                        "text": s.text.strip(), "words": words})
    return out


def read_wav_window(wav_path, start, end):
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        w.setpos(min(int(start * sr), w.getnframes()))
        raw = w.readframes(int((end - start) * sr))
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(wav_path, model_size, lang="en"):
    """Transcribe the WHOLE video. Slow on long videos - prefer transcribe_windows."""
    model = load_whisper(model_size)
    with wave.open(str(wav_path), "rb") as w:
        # Pass raw samples (a 16 kHz mono wav) so faster-whisper skips its own audio
        # decoder, which breaks with some newer PyAV versions.
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return _run_whisper(model, audio.astype(np.float32) / 32768.0, 0.0, lang)


def mmss(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def merge_windows(wins):
    out = []
    for a, b in sorted(wins):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def cache_load(video):
    """Analysis is saved next to the video so re-running (new layout, title...) is quick."""
    path = video.parent / (video.name + ".clipcache.json")
    st = video.stat()
    key = f"{st.st_size}-{int(st.st_mtime)}"
    try:
        d = json.loads(path.read_text())
        if d.get("key") == key:
            return path, d
    except Exception:
        pass
    return path, {"key": key}


def cache_save(path, cache):
    try:
        path.write_text(json.dumps(cache))
    except OSError:
        pass


def transcribe_windows(get_wav, windows, model_size, lang, cache, cpath):
    """Transcribe only the given (start, end) second ranges. Finished ranges are cached."""
    store = cache.setdefault("transcripts", {})
    model, out = None, []
    for k, (a, b) in enumerate(windows, 1):
        key = f"{model_size}|{lang}|{a}-{b}"
        if key not in store:
            if model is None:
                model = load_whisper(model_size)
            print(f"  transcribing part {k}/{len(windows)}  ({mmss(a)} - {mmss(b)})", flush=True)
            store[key] = _run_whisper(model, read_wav_window(get_wav(), a, b), a, lang)
            cache_save(cpath, cache)
        out.extend(store[key])
    return out


def hype_scores(segments, n_secs):
    score = np.zeros(n_secs, dtype=np.float32)
    for seg in segments:
        for sec, v in line_scores(seg["words"]).items():
            if 0 <= sec < n_secs:
                score[sec] += v
    return score


# ---------- chat (viewer messages) ----------
CHAT_PHRASES = [
    "what the fuck", "what the hell", "wtf", "wth", "omg", "omfg", "oh my god", "lmao",
    "lmfao", "kekw", "omegalul", "lul", "pog", "poggers", "pogchamp", "clip it", "clip that",
    "lets go", "let's go", "letsgo", "no way", "holy shit", "holy fuck", "im dead", "i'm dead",
    "dying", "crying", "insane", "crazy", "sheesh", "bruh", "gg", "w", "cooked", "unreal",
]
CHAT_EMOJI = ["\U0001F480", "\U0001F602", "\U0001F923", "\U0001F62D", "\U0001F525",
              "\U0001F631", "\U0001F92F"]
_CHAT_WORDS = re.compile(r"(?<![a-z])(" + "|".join(re.escape(p) for p in CHAT_PHRASES)
                         + r")(?![a-z])")
_CHAT_STRETCH = re.compile(r"\b(lo+l+|n+o{2,}|a{4,}h*|ha(ha)+|lma+o+)\b")


def is_hype_message(text):
    t = text.lower()
    return bool(_CHAT_WORDS.search(t) or _CHAT_STRETCH.search(t)
                or any(e in t for e in CHAT_EMOJI))


def parse_chat(path):
    """Return [(seconds, text)] from a YouTube live_chat replay (one JSON object per line)
    or a TwitchDownloader JSON export (a "comments" list)."""
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    msgs = []
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and "comments" in data:  # Twitch
        for c in data["comments"]:
            body = (c.get("message") or {}).get("body", "")
            msgs.append((float(c.get("content_offset_seconds", 0)), body))
        return msgs
    for line in raw.splitlines():  # YouTube
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        r = obj.get("replayChatItemAction") or {}
        try:
            t = int(r.get("videoOffsetTimeMsec")) / 1000.0
        except (TypeError, ValueError):
            continue
        for act in r.get("actions", []):
            item = (act.get("addChatItemAction") or {}).get("item", {})
            rend = (item.get("liveChatTextMessageRenderer")
                    or item.get("liveChatPaidMessageRenderer"))
            if not rend:
                continue
            text = ""
            for run in (rend.get("message") or {}).get("runs", []):
                if "text" in run:
                    text += run["text"]
                elif "emoji" in run:
                    eid = run["emoji"].get("emojiId", "")
                    text += eid if len(eid) <= 8 else " "
            msgs.append((t, text))
    return msgs


def chat_scores(msgs, n_secs):
    """Chat-speed spikes + share of hype messages, per second."""
    total = np.zeros(n_secs, dtype=np.float32)
    hype = np.zeros(n_secs, dtype=np.float32)
    for t, text in msgs:
        i = int(t)
        if 0 <= i < n_secs:
            total[i] += 1
            if is_hype_message(text):
                hype[i] += 1
    smooth = np.convolve(total, np.ones(3) / 3, mode="same")
    z = np.clip((smooth - smooth.mean()) / (smooth.std() + 1e-6), 0, 10)
    return 0.6 * z + 2.0 * np.log1p(hype)


def visual_change_scores(video, n_secs, sample_fps=0.5):
    """Scan the full video locally for abrupt visual-change candidates.

    This signal finds where to look; it does not claim that every cut or flash
    is exciting. Frames are streamed at low resolution to keep memory bounded.
    """
    width, height = 128, 72
    frame_bytes = width * height
    interval = 1.0 / sample_fps
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i",
           str(Path(video).resolve()), "-vf",
           f"fps={sample_fps},scale={width}:{height}:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    times, changes, previous = [], [], None
    try:
        index = 0
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) != frame_bytes:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).astype(np.int16)
            if previous is not None:
                changes.append(float(np.mean(np.abs(frame - previous))))
                times.append(index * interval)
            previous = frame
            index += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        code = proc.wait()
        if proc.stderr:
            proc.stderr.close()
    if code != 0:
        raise RuntimeError(f"ffmpeg visual scan failed: {err[-1200:]}")
    out = np.zeros(n_secs, dtype=np.float32)
    if not changes:
        return out
    vals = np.asarray(changes, dtype=np.float32)
    for i, (sec, value) in enumerate(zip(times, vals)):
        baseline = vals[max(0, i - 30):i]
        med = float(np.median(baseline)) if len(baseline) >= 5 else float(np.median(vals))
        mad = float(np.median(np.abs(baseline - med))) * 1.4826 if len(baseline) >= 5 else 0.0
        score = float(np.clip((value - med) / max(mad, 4.0) * 5.0, 0, 5))
        second = int(sec)
        if 0 <= second < n_secs:
            out[second] = max(out[second], score)
    return out


RUBRIC_WEIGHTS = {
    "hook": 0.20, "context": 0.15, "intensity": 0.20, "payoff": 0.15,
    "pacing": 0.10, "audio_evidence": 0.08, "visual_evidence": 0.08,
    "transcript_confidence": 0.04,
}


def parse_rubric_weights(text):
    """Parse a JSON map of rubric dimensions to nonnegative weights."""
    if not text:
        return dict(RUBRIC_WEIGHTS)
    try:
        values = json.loads(text)
        if not isinstance(values, dict) or set(values) - set(RUBRIC_WEIGHTS):
            raise ValueError
        weights = dict(RUBRIC_WEIGHTS)
        for name, value in values.items():
            value = float(value)
            if not np.isfinite(value) or value < 0:
                raise ValueError
            weights[name] = value
        if not any(weights.values()):
            raise ValueError
        return weights
    except (ValueError, TypeError, json.JSONDecodeError):
        sys.exit("--rubric-weights must be a JSON object of known rubric dimensions and nonnegative numbers.")


def gemini_review(video, moments, segments, workdir, model, rubric_weights=None):
    """Optionally score candidate transcripts and a few sampled frames with Gemini.

    Only selected JPEG frames and the nearby transcript are sent; the source
    video is never uploaded. Missing keys/dependencies/errors return the
    original candidates unchanged.
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("  Gemini review skipped: set GEMINI_API_KEY to enable it.")
        return moments
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("  Gemini review skipped: install it with: python -m pip install google-genai")
        return moments
    if not moments:
        return moments
    rubric_weights = rubric_weights or RUBRIC_WEIGHTS

    # Bound API cost and image upload volume on long recordings.
    review_limit = min(12, len(moments))
    parts = []
    brief = []
    for i, m in enumerate(moments[:review_limit]):
        start, end, peak = float(m["start"]), float(m["end"]), float(m["peak"])
        nearby = [w for seg in segments for w in seg.get("words", [])
                  if start <= float(w.get("start", -1)) <= end]
        transcript = " ".join(w.get("word", "") for w in nearby)
        brief.append({
            "candidate": i, "start": round(start, 2), "end": round(end, 2),
            "peak": round(peak, 2), "detectors": m.get("sources", []),
            "signal_scores": {k: m.get(k) for k in ("loud", "speech", "chat", "visual")},
            "transcript": transcript[:1800],
        })
        for label, sec in (("before", max(0.0, start + 0.5)),
                           ("event", peak),
                           ("after", max(start, end - 0.5))):
            jpg = Path(workdir) / f"gemini_{i}_{label}.jpg"
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss",
                   f"{sec:.2f}", "-i", str(Path(video).resolve()), "-frames:v", "1",
                   "-vf", "scale=640:-2", "-q:v", "5", "-y", str(jpg)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and jpg.exists() and jpg.stat().st_size:
                parts.append(types.Part.from_bytes(data=jpg.read_bytes(), mime_type="image/jpeg"))
                parts.append(f"Candidate {i}, {label} frame at {sec:.2f}s.")
                try:
                    jpg.unlink()
                except OSError:
                    pass

    prompt = (
        "Review the candidate records and their labeled sampled frames for short-form "
        "gameplay clips. The audio/chat and visual detectors nominate candidates independently; "
        "do not require detector agreement. Do not invent events unsupported by transcript or "
        "frames. Score each dimension 0-5, or null when evidence is unavailable: "
        "hook, context, intensity, payoff, pacing, audio_evidence, visual_evidence, "
        "transcript_confidence. Return JSON only: {\"candidates\":[{\"candidate\": integer, "
        "\"scores\":{dimension:number|null}, \"explanation\":string}]}. "
        "Use the candidate index exactly. Candidate records: " + json.dumps(brief, ensure_ascii=False)
    )
    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model, contents=[prompt, *parts],
            config={"response_mime_type": "application/json"})
        data = json.loads(response.text or "{}")
        reviewed = {int(x["candidate"]): x for x in data.get("candidates", [])}
        for i, moment in enumerate(moments):
            item = reviewed.get(i)
            if not item:
                continue
            scores = item.get("scores", {})
            valid = {}
            for name, weight in rubric_weights.items():
                value = scores.get(name)
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(value):
                    valid[name] = min(5.0, max(0.0, value))
            if valid:
                denom = sum(rubric_weights[k] for k in valid)
                moment["rubric_scores"] = valid
                moment["rubric_score"] = round(
                    20.0 * sum(valid[k] * rubric_weights[k] for k in valid) / denom, 2)
                moment["rubric_explanation"] = str(item.get("explanation", ""))[:600]
        return sorted(moments, key=lambda m: (-m.get("rubric_score", -1), -m["rank"]))
    except Exception as e:
        print(f"  Gemini review failed; keeping detector ranking: {e}")
        return moments


# ---------- picking moments and trimming clips ----------
def normalize_signal(values):
    """Map one noisy signal to a comparable 0..5 robust prominence scale."""
    arr = np.asarray(values, dtype=np.float32)
    finite = np.isfinite(arr)
    if not finite.any():
        return np.zeros_like(arr)
    clean = arr[finite]
    med = float(np.median(clean))
    p90 = float(np.percentile(clean, 90))
    scale = p90 - med
    if scale < 1e-6:
        mad = float(np.median(np.abs(clean - med))) * 1.4826
        scale = mad if mad >= 1e-6 else float(np.max(clean) - med)
    if scale < 1e-6:
        return np.zeros_like(arr)
    out = np.zeros_like(arr)
    out[finite] = np.clip((arr[finite] - med) / scale * 5.0, 0.0, 5.0)
    return out


def pick_moments(parts, count, length, min_score, weights=None):
    """parts: {'loud':…, 'speech':…, 'chat':…} per-second arrays. Moments where several
    independent signals agree are ranked higher. Each signal is robustly normalized
    before blending so raw scale differences do not decide the ranking. Returns
    (moments, smoothed_total)."""
    k = np.ones(5) / 5
    weights = weights or {}
    sm = {name: np.convolve(normalize_signal(arr), k, mode="same") *
          max(0.0, float(weights.get(name, 1.0)))
          for name, arr in parts.items()
          if float(weights.get(name, 1.0)) > 0}
    # Independent detectors can nominate a moment on their own. Agreement adds
    # a smaller bonus; a quiet streamer does not veto a visual event.
    matrix = np.stack(list(sm.values())) if sm else np.zeros((1, 0), dtype=np.float32)
    strongest = np.max(matrix, axis=0)
    total = strongest + 0.25 * (np.sum(matrix, axis=0) - strongest)
    sig = np.zeros(len(total), dtype=int)
    if "loud" in sm:
        sig += (sm["loud"] >= 1.5).astype(int)
    if "speech" in sm:
        sig += (sm["speech"] >= 0.3).astype(int)
    if "chat" in sm:
        sig += (sm["chat"] >= 1.0).astype(int)
    if "visual" in sm:
        sig += (sm["visual"] >= 1.5).astype(int)
    rank = total * (1 + 0.3 * np.maximum(sig - 1, 0))
    work = rank.copy()
    out = []
    while len(out) < count:
        peak = int(np.argmax(work))
        if work[peak] <= 0 or total[peak] < min_score:
            break
        m = {"peak": peak, "score": round(float(total[peak]), 2),
             "rank": round(float(rank[peak]), 2), "signals": int(sig[peak])}
        m.update({name: round(float(v[peak]), 2) for name, v in sm.items()})
        m["sources"] = [name for name, values in sm.items() if values[peak] >= 1.0]
        out.append(m)
        work[max(0, peak - length):peak + length + 1] = -1  # clips never overlap
    return out, total


def _extent(sm, peak, thr, step, limit, gap=3):
    """Walk away from the peak while the action continues (short lulls are allowed)."""
    i, last_ok, misses = peak, peak, 0
    while 0 <= i + step < len(sm) and abs(i + step - peak) <= limit:
        i += step
        if sm[i] >= thr:
            last_ok, misses = i, 0
        else:
            misses += 1
            if misses > gap:
                break
    return last_ok


def refine_window(sm, peak, max_len, min_len, pre, total_secs, post=3.0):
    """Start a few seconds before the action begins and end just after it dies down,
    instead of using a fixed-length window."""
    thr = 0.35 * float(sm[peak])
    onset = _extent(sm, peak, thr, -1, max_len)
    offset = _extent(sm, peak, thr, +1, max_len)
    start = max(0.0, onset - pre)
    end = min(total_secs, offset + post)
    if end - start < min_len:
        end = min(total_secs, start + min_len)
        if end - start < min_len:
            start = max(0.0, end - min_len)
    if end - start > max_len:
        start = max(start, peak - 0.4 * max_len)
        end = min(total_secs, start + max_len)
    return {"start": float(start), "end": float(end)}


def snap_start(clip, segments, max_shift=3.0):
    """Begin at the start of a sentence instead of mid-word."""
    best = None
    for s in segments:
        d = abs(s["start"] - clip["start"])
        if d <= max_shift and (best is None or d < best[0]):
            best = (d, s["start"])
    if best:
        clip["start"] = max(0.0, best[1] - 0.25)
    return clip


snap_to_speech = snap_start  # old name


def snap_end(clip, segments, max_shift=3.5):
    """Finish when a sentence finishes so the clip doesn't cut off mid-reaction."""
    best = None
    for s in segments:
        d = s["end"] - clip["end"]
        if -1.5 <= d <= max_shift and (best is None or abs(d) < best[0]):
            best = (abs(d), s["end"])
    if best:
        clip["end"] = best[1] + 0.35
    return clip


def plan_clip(peak, sm, total_secs, segments, max_len, min_len, pre):
    """Final start/end for one moment."""
    clip = refine_window(sm, peak, max_len, min_len, pre, total_secs)
    if segments:
        snap_start(clip, segments)
        snap_end(clip, segments)
    clip["start"] = max(0.0, clip["start"])
    clip["end"] = min(total_secs, clip["end"])
    if clip["end"] - clip["start"] < min_len:
        clip["end"] = min(total_secs, clip["start"] + min_len)
    if clip["end"] - clip["start"] > max_len + 3:
        clip["end"] = clip["start"] + max_len + 3
    return clip


# ---------- look: captions, title, layouts ----------
# Everything is drawn with Pillow as PNG images and overlaid with ffmpeg's core `overlay`
# filter, so it works on any ffmpeg build (some, like Homebrew's, lack libass/drawtext).
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
CAP_W, CAP_H = 1080, 300
CAPTION_YELLOW = (255, 214, 0)
LAYOUT_CAP_Y = {"fit": 1360, "square": 1650, "stack": 1325, "crop": 1500}  # caption centre
LAYOUTS = ["fit", "square", "stack", "crop"]


@lru_cache(maxsize=None)
def _font(size):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _pil():
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("Pillow missing. Run: pip install -r requirements.txt")
    return Image, ImageDraw


def _chunks(words, group=3, max_gap=0.7):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= group or w["start"] - cur[-1]["end"] > max_gap
                    or re.search(r"[.!?]$", cur[-1]["word"])):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def _draw_chunk(chunk, active):
    """A few words on screen with the word being spoken right now in yellow."""
    Image, ImageDraw = _pil()
    texts = [w["word"].upper() for w in chunk]
    size = 92
    while True:
        font = _font(size)
        space = font.getlength(" ")
        widths = [font.getlength(t) for t in texts]
        total = sum(widths) + space * (len(texts) - 1)
        if total <= CAP_W - 80 or size <= 40:
            break
        size -= 6
    img = Image.new("RGBA", (CAP_W, CAP_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = (CAP_W - total) / 2
    for k, t in enumerate(texts):
        d.text((x, CAP_H / 2), t, font=font, anchor="lm",
               fill=CAPTION_YELLOW if k == active else "white",
               stroke_width=8, stroke_fill="black")
        x += widths[k] + space
    return img


def make_captions(segments, start, end, outdir, layout="fit"):
    """Word-by-word highlighted captions as ONE timed overlay stream (an ffmpeg concat
    list of images). Returns the list file path, or None when nobody speaks."""
    Image, _ = _pil()
    words = [w for s in segments for w in s["words"] if start <= w["start"] < end]
    if not words:
        return None
    outdir.mkdir(parents=True, exist_ok=True)
    blank = outdir / "blank.png"
    Image.new("RGBA", (CAP_W, CAP_H), (0, 0, 0, 0)).save(blank)
    spans = []
    for chunk in _chunks(words):
        for k, w in enumerate(chunk):
            a = w["start"] - start
            b = (chunk[k + 1]["start"] - start) if k + 1 < len(chunk) else w["end"] - start + 0.15
            png = outdir / f"c_{len(spans):04d}.png"
            _draw_chunk(chunk, k).save(png)
            spans.append((png, max(0.0, a), b))
    lines, cursor = [], 0.0
    for png, a, b in spans:
        a = max(a, cursor)
        if b <= a + 0.02:
            continue
        if a > cursor + 0.01:
            lines.append((blank, a - cursor))
        lines.append((png, b - a))
        cursor = b
    lines.append((blank, 0.5))
    lst = outdir / "captions.txt"
    def quote_concat_path(path):
        return str(path.resolve()).replace("'", "'\\''")

    with open(lst, "w") as f:
        for png, dur in lines:
            f.write(f"file '{quote_concat_path(png)}'\nduration {dur:.3f}\n")
        f.write(f"file '{quote_concat_path(blank)}'\n")  # repeat final file for concat
    return lst


def make_brand(handle, path, layout="fit"):
    """Full-frame transparent overlay with the channel handle at the bottom."""
    Image, ImageDraw = _pil()
    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if handle:
        d.text((540, 1885 if layout == "stack" else 1855), handle, font=_font(42),
               fill="white", anchor="mm", stroke_width=5, stroke_fill="black")
    img.save(path)
    return path


def layout_graph(layout, cam, crop_x=0.5):
    """Build a 1080x1920 portrait graph without stretching the source.

    ``fit`` is the safe default: it keeps the entire source visible over a
    blurred background. ``crop`` can be biased horizontally with crop_x.
    """
    blur = ("scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            "boxblur=8:2,scale=1080:1920")  # blur a small copy: looks the same, renders faster
    if layout == "crop":
        # Scale to cover the portrait canvas, then choose the crop's horizontal
        # anchor instead of assuming the streamer is at the exact center.
        x = min(1.0, max(0.0, float(crop_x)))
        return ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920:x='(iw-1080)*{x:.4f}':y='(ih-1920)/2',setsar=1[v0]")
    if layout == "square":  # bigger, tighter view of the action (centre square crop)
        return (f"[0:v]split=2[bg][fg];[bg]{blur}[b];"
                "[fg]crop='min(iw,ih)':'min(iw,ih)',scale=1080:1080[f];"
                "[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    if layout == "stack":  # gameplay on top, streamer's facecam underneath
        cx, cy, cw, ch = cam
        return (f"[0:v]split=3[bg][g][c];[bg]{blur}[b];"
                "[g]crop='min(iw,ih)':'min(iw,ih)',scale=1080:1080[g1];"
                f"[c]crop=iw*{cw}:ih*{ch}:iw*{cx}:ih*{cy},"
                "scale=1080:520:force_original_aspect_ratio=decrease[c1];"
                "[b][g1]overlay=0:230[t];[t][c1]overlay=(W-w)/2:1340+(520-h)/2,setsar=1[v0]")
    return (f"[0:v]split=2[bg][fg];[bg]{blur}[b];[fg]scale=1080:-2[f];"
            "[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")  # fit: whole frame, blurred sides


def render(video, clip, out_path, layout, captions, workdir, brand=None, cam=None,
           crop_x=0.5):
    dur = clip["end"] - clip["start"]
    graph = layout_graph(layout, cam, crop_x)
    source_dims = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
                       str(Path(video).resolve())]).strip()
    cmd = ["ffmpeg", "-y", "-ss", f"{clip['start']:.2f}", "-i", str(Path(video).resolve())]
    idx, last = 1, "[v0]"
    if captions:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(Path(captions).resolve())]
        graph += (f";{last}[{idx}:v]overlay=0:{LAYOUT_CAP_Y[layout] - CAP_H // 2}"
                  f":eof_action=pass[v{idx}]")
        last, idx = f"[v{idx}]", idx + 1
    if brand:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{dur:.2f}",
                "-i", str(Path(brand).resolve())]
        graph += f";{last}[{idx}:v]overlay=0:0[v{idx}]"
        last, idx = f"[v{idx}]", idx + 1
    fade_at = max(0.0, dur - 0.3)
    graph += f";{last}fade=t=out:st={fade_at:.2f}:d=0.3[v]"
    cmd += ["-t", f"{dur:.2f}", "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-af", f"afade=t=in:st=0:d=0.08,afade=t=out:st={fade_at:.2f}:d=0.3",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-fps_mode", "cfr",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(Path(out_path).resolve())]
    run(cmd, cwd=workdir)
    # Fail loudly if an encoder/filter change ever produces a non-portrait file.
    probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
                 str(Path(out_path).resolve())])
    try:
        width, height = (int(x) for x in probe.strip().split("x", 1))
    except (TypeError, ValueError):
        sys.exit(f"Could not read output dimensions for {out_path}: {probe.strip()!r}")
    if (width, height) != (1080, 1920):
        sys.exit(f"Unexpected output dimensions for {out_path}: {width}x{height}; expected 1080x1920")
    print(f"  Frame: source {source_dims or 'unknown'} -> {width}x{height} "
          f"(9:16), layout={layout}" +
          (f", crop-x={crop_x:.2f}" if layout == "crop" else ""), flush=True)


def parse_cam(text):
    try:
        v = tuple(float(x) for x in text.split(":"))
        assert len(v) == 4 and all(0 <= x <= 1 for x in v) and v[0] + v[2] <= 1.001 \
            and v[1] + v[3] <= 1.001
        return v
    except Exception:
        sys.exit("--cam must be x:y:w:h as fractions of the frame, e.g. 0.72:0.62:0.28:0.38 "
                 "for a facecam in the bottom-right corner")


def main():
    t_start = time.time()
    p = argparse.ArgumentParser(description="Auto-clip hype moments into vertical clips.")
    p.add_argument("video", help="video file path OR a link (Twitch VOD, YouTube, etc.)")
    p.add_argument("--count", type=int, default=8, help="max clips (default 8)")
    p.add_argument("--length", type=int, default=30, help="longest a clip can be (default 30)")
    p.add_argument("--min-length", type=int, default=15, help="shortest a clip can be (default 15)")
    p.add_argument("--lead", type=int, default=5,
                   help="seconds of set-up before the action starts (default 5)")
    p.add_argument("--min-score", type=float, default=1.5, help="raise for pickier clips")
    p.add_argument("--reframe", choices=LAYOUTS, default="fit",
                   help="fit = whole frame, square = bigger centre crop, "
                        "stack = gameplay + facecam (needs --cam), crop = fill screen")
    p.add_argument("--cam", help="facecam area as x:y:w:h fractions of the frame (for stack)")
    p.add_argument("--crop-x", type=float, default=0.5,
                   help="horizontal crop anchor from 0 (left) to 1 (right), for --reframe crop")
    p.add_argument("--handle", default="@gtasixviralclips",
                   help="Instagram/channel handle shown at the bottom")
    p.add_argument("--no-brand", action="store_true", help="don't show the bottom handle")
    p.add_argument("--model", default="base", help="whisper model: tiny/base/small/medium")
    p.add_argument("--lang", default="en", help="speech language code, or 'auto'")
    p.add_argument("--no-transcribe", action="store_true", help="audio-only, no captions")
    p.add_argument("--candidates", type=int,
                   help="how many promising parts to transcribe (default: 3x --count, min 15)")
    p.add_argument("--full-transcribe", action="store_true",
                   help="transcribe the WHOLE video (can take hours on long videos)")
    p.add_argument("--quality", type=int, default=1080,
                   help="max video height when downloading a link (720 downloads faster)")
    p.add_argument("--out", default="clips")
    p.add_argument("--chat-file", help="chat export to use (YouTube .live_chat.json or "
                                        "TwitchDownloader .json)")
    p.add_argument("--no-chat", action="store_true", help="don't fetch/use viewer chat")
    p.add_argument("--chat-weight", type=float, default=1.0, help="how much chat counts")
    p.add_argument("--visual-scan", action="store_true",
                   help="independently scan the whole video for visual-change candidates (local, no API)")
    p.add_argument("--visual-fps", type=float, default=0.5,
                   help="visual scan sampling rate in frames/sec (default 0.5; max 2)")
    p.add_argument("--gemini-review", action="store_true",
                   help="rank a larger candidate pool with Gemini using transcript excerpts and sampled frames; sends these to Google")
    p.add_argument("--gemini-model", default="gemini-3.8-flash",
                   help="Gemini model for --gemini-review")
    p.add_argument("--rubric-weights",
                   help='JSON weight overrides, e.g. {"hook":0.25,"intensity":0.25}')
    p.add_argument("--upload-youtube", action="store_true", help="upload each clip to YouTube")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private",
                   help="YouTube privacy (default private so you can review first)")
    p.add_argument("--title-prefix", default="Json", help="YouTube title prefix")
    p.add_argument("--tags", default="gta 6,gta vi,gaming,funny moments",
                   help="comma-separated YouTube tags")
    a = p.parse_args()
    lang = None if a.lang == "auto" else a.lang

    need("ffmpeg")
    need("ffprobe")
    cam = parse_cam(a.cam) if a.cam else None
    if not 0 <= a.crop_x <= 1:
        sys.exit("--crop-x must be between 0 (left) and 1 (right). Use --reframe fit to keep the full source visible.")
    if not 0 < a.visual_fps <= 2:
        sys.exit("--visual-fps must be greater than 0 and at most 2.")
    rubric_weights = parse_rubric_weights(a.rubric_weights)
    if a.gemini_review:
        # Gemini review includes visual evidence, so also generate candidates
        # from the independent local visual scan unless already requested.
        a.visual_scan = True
    if a.reframe == "stack" and not cam:
        sys.exit("The stack layout needs --cam x:y:w:h (e.g. --cam 0.72:0.62:0.28:0.38).")
    if a.video.startswith(("http://", "https://")):
        video, chat_path = download(a.video, Path("downloads"), not a.no_chat, a.quality)
    else:
        video, chat_path = Path(a.video), None
    if a.chat_file and not a.no_chat:
        chat_path = Path(a.chat_file)
    if not video.exists():
        sys.exit(f"File not found: {video}")
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_secs = duration(video)
    n_secs = int(total_secs)
    a.length = min(a.length, n_secs)
    a.min_length = min(a.min_length, a.length)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cpath, cache = cache_load(video)
        wav_box = {}

        def get_wav():  # extracted only when really needed, and only once
            if "p" not in wav_box:
                print("Extracting audio...", flush=True)
                wav = tmp / "audio.wav"
                run(["ffmpeg", "-y", "-i", str(video.resolve()), "-vn", "-ac", "1",
                     "-ar", "16000", "-f", "wav", str(wav)])
                wav_box["p"] = wav
            return wav_box["p"]

        if len(cache.get("loud", [])) == n_secs:
            print("Using the saved loudness analysis for this video.")
            loud = np.array(cache["loud"], dtype=np.float32)
        else:
            wav = get_wav()
            print("Analysing loudness...", flush=True)
            loud = loudness_scores(wav, n_secs)
            cache["loud"] = [round(float(x), 2) for x in loud]
            cache_save(cpath, cache)
        parts = {"loud": loud}
        if a.visual_scan:
            print(f"Scanning whole video for visual changes at {a.visual_fps:g} frames/sec...",
                  flush=True)
            try:
                parts["visual"] = visual_change_scores(video, n_secs, a.visual_fps)
                print(f"  Visual scan found {int(np.count_nonzero(parts['visual']))} candidate seconds.")
            except Exception as e:
                print(f"  Visual scan unavailable; continuing with audio/chat: {e}")
        if chat_path and not a.no_chat:
            msgs = parse_chat(chat_path)
            print(f"Chat: {len(msgs)} messages")
            if msgs:
                parts["chat"] = a.chat_weight * chat_scores(msgs, n_secs)
        elif not a.no_chat:
            print("No chat found; using audio and speech only.")

        segments = []
        if not a.no_transcribe:
            if a.full_transcribe:
                print(f"Transcribing the WHOLE video with Whisper ({a.model})... "
                      "this can take hours", flush=True)
                segments = transcribe(get_wav(), a.model, lang)
            else:
                # Shortlist from independent audio/chat/visual detectors, then
                # transcribe only those regions and rank again with speech evidence.
                n_cand = a.candidates or max(15, 3 * a.count)
                cands, ctotal = pick_moments(
                    parts, n_cand, a.length, 0.5, {"chat": a.chat_weight})
                wins = []
                for m in cands:
                    c = plan_clip(m["peak"], ctotal, total_secs, [], a.length, a.min_length, a.lead)
                    wins.append((max(0, int(c["start"]) - 6), min(n_secs, int(c["end"]) + 6)))
                wins = merge_windows(wins)
                mins = sum(b - a for a, b in wins) / 60
                print(f"Transcribing only the {len(wins)} most promising parts "
                      f"({mins:.0f} min of {total_secs / 60:.0f} min)...", flush=True)
                segments = transcribe_windows(get_wav, wins, a.model, lang, cache, cpath)
            parts["speech"] = hype_scores(segments, n_secs)

        pool_size = max(a.count, min(50, a.candidates or 3 * a.count))
        moments, total = pick_moments(
            parts, pool_size, a.length, a.min_score, {"chat": a.chat_weight})
        if not moments:
            sys.exit("No moments passed the threshold. Try a lower --min-score.")
        # Freeze locally computed, source-bounded clip windows before optional
        # model review. The model can rank candidates but cannot set timestamps.
        for moment in moments:
            window = plan_clip(moment["peak"], total, total_secs, segments,
                               a.length, a.min_length, a.lead)
            moment.update(window)
        if a.gemini_review:
            moments = gemini_review(video, moments, segments, tmp, a.gemini_model,
                                    rubric_weights)
        all_candidates = moments[:]
        moments = moments[:a.count]
        print(f"Found {len(moments)} moments (best first). Rendering...", flush=True)

        brand = None
        if not a.no_brand:
            brand = make_brand(a.handle, tmp / "brand.png", a.reframe)

        clips = []
        for i, m in enumerate(moments, 1):
            c = plan_clip(m["peak"], total, total_secs, segments, a.length, a.min_length, a.lead)
            c.update(m)
            caps = (make_captions(segments, c["start"], c["end"], tmp / f"caps_{i}", a.reframe)
                    if segments else None)
            name = out_dir / f"clip_{i:02d}_{int(c['start'])}s.mp4"
            render(video, c, name, a.reframe, caps, tmp, brand, cam, a.crop_x)
            c["file"] = str(name)
            clips.append(c)
            print(f"  {name.name}  {c['end'] - c['start']:.0f}s  (rank {c['rank']}, "
                  f"{c['signals']} signal{'s' if c['signals'] != 1 else ''})", flush=True)
            if a.upload_youtube:
                try:
                    from youtube_upload import upload_video
                    vid = upload_video(name, f"{a.title_prefix} #{i}", a.privacy,
                                       [t.strip() for t in a.tags.split(",") if t.strip()])
                    c["youtube"] = f"https://youtu.be/{vid}"
                    print(f"    uploaded ({a.privacy}): {c['youtube']}")
                except Exception as e:  # e.g. daily quota hit: keep clips, stop uploading
                    print(f"    YouTube upload failed, skipping further uploads: {e}")
                    a.upload_youtube = False

    (out_dir / "clips.json").write_text(json.dumps(clips, indent=2))
    (out_dir / "candidates.json").write_text(json.dumps(all_candidates, indent=2))
    print(f"Done in {(time.time() - t_start) / 60:.1f} min. Clips are in ./{out_dir}/  "
          "(clip_01 is the best)")


if __name__ == "__main__":
    main()
