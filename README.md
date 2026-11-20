# Stream to Shorts

Local-first Python tools that find promising gameplay moments and render vertical clips for YouTube Shorts and Instagram Reels.

![Demo GIF placeholder — add docs/demo.gif](docs/demo.gif)

## Quick start

### Prerequisites

- Python 3.12 or newer
- FFmpeg and `ffprobe` available on your `PATH`
- Enough free disk space for the source video, analysis cache, Whisper model, and rendered clips

Check the tools:

```bash
python3 --version
ffmpeg -version
ffprobe -version
```

Install the pinned dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Run the clipper on a local recording or supported URL:

```bash
python clipper.py gameplay.mp4 --visual-scan --count 5
```

Or start the local web UI:

```bash
python web_app.py
```

Open <http://127.0.0.1:8765>, upload an MP4 or paste a YouTube, Twitch, or Kick VOD link, choose how many shorts to generate, then click **Generate Shorts**. The page shows processing stages, elapsed time, and the resulting playable/downloadable clips. It keeps the current job visible after a browser refresh while the server remains running. Do not open `web/index.html` directly; run `web_app.py` so the UI can reach the backend. The server binds to localhost by default and is not configured for public internet access.

Rendered clips and `clips/candidates.json` are written to `clips/`. The default layout is portrait 9:16 at 1080×1920. Captions are burned in when a transcript is available; use `--no-captions` to omit them.

For an analysis-only run that does not render or upload clips:

```bash
python clipper.py gameplay.mp4 --visual-scan --count 5 --dry-run
```

This writes the candidate list to `clips/candidates.json` and keeps analysis caches beside the source video.

## Review candidates before rendering

Generate a candidate list, then open the local review page:

```bash
python clipper.py gameplay.mp4 --visual-scan --count 5 --dry-run
python preview.py gameplay.mp4 clips/candidates.json
```

Open the `http://127.0.0.1:...` address printed by the preview command. Play each range, approve the candidates you want, and save the selection. The page writes `clips/approved.json` by default. Stop the server with Ctrl+C, then render the approved candidates:

```bash
python clipper.py gameplay.mp4 --approved-json clips/approved.json
```

The export checks the video’s SHA-256 against the approval manifest. If the source changed, make a new preview. The renderer reuses cached analysis and transcribes approved ranges when captions are enabled.

## Detection and ranking

The detectors can nominate moments independently:

- Audio loudness detects sudden increases relative to the recording.
- Transcript scoring looks for reaction words, phrases, and repeated words in the faster-whisper transcript.
- Chat scoring uses message rate and a list of hype words and emotes when a chat replay or supported live chat is available.
- Visual scanning measures frame-to-frame changes. The score can increase with local audio energy, and triggers within two seconds are reduced to the strongest one.

Signals are normalized and merged. Agreement adds a ranking bonus; it is not required for a candidate to qualify. The current default weights and thresholds are in [`config.toml`](config.toml). Candidate timestamps and scores are saved in `clips/candidates.json`.

Gemini review is optional and runs only with `--gemini-review`. It ranks up to 12 locally detected candidates using transcript excerpts and up to three sampled frames per candidate. It may reorder candidates but cannot create candidates or change their timestamps. The source video is not sent. Install the optional SDK and provide an API key:

```bash
python -m pip install '.[gemini]'
export GEMINI_API_KEY="your-api-key"
python clipper.py gameplay.mp4 --visual-scan --gemini-review --count 5
```

See [Google’s Gemini API documentation](https://ai.google.dev/gemini-api/docs) for API setup and account usage details. A Gemini subscription and Gemini API access are separate.

## Output framing

The output is H.264/AAC MP4 in a 1080×1920 frame.

```bash
# Preserve the whole source over a blurred background
python clipper.py gameplay.mp4 --reframe fit

# Stack gameplay and a facecam (rectangle uses normalized x:y:w:h fractions)
python clipper.py gameplay.mp4 --reframe stack --cam 0.72:0.62:0.28:0.38

# Fill the portrait frame with a horizontally shifted crop
python clipper.py gameplay.mp4 --reframe crop --crop-x 0.7
```

The bottom handle defaults to `@gtasixviralclips`; change it with `--handle` or omit it with `--no-brand`. The video title is not overlaid.

## Run the web UI with Docker

```bash
docker compose up --build ui
```

Then open <http://127.0.0.1:8765>. Uploaded videos and generated clips persist under `jobs/`; downloaded speech models persist under `models/`. The UI service is published only on localhost by default.

## Run with Docker

Docker Desktop or a Docker Engine must be running. Build the image from the repository:

```bash
docker build -t stream-to-shorts:local .
mkdir -p input clips downloads live models
```

Run `clipper.py` on a mounted local recording:

```bash
docker run --rm \
  -v "$PWD/input:/data/input:ro" \
  -v "$PWD/clips:/data/clips" \
  -v "$PWD/downloads:/data/downloads" \
  -v "$PWD/models:/data/models" \
  stream-to-shorts:local /data/input/gameplay.mp4 --visual-scan --count 5
```

Run `live.py` on a live stream. Live session recordings and clips are kept under the mounted `live/` directory:

```bash
docker run --rm --init --entrypoint python \
  -v "$PWD/live:/data/live" \
  -v "$PWD/models:/data/models" \
  stream-to-shorts:local /data/live.py "LIVE_STREAM_URL" --auto-clip --visual-scan --count 5
```

The container runs as a non-root user and includes FFmpeg. Source media, downloads, clips, sessions, and model files are host-mounted; they are not baked into the image. The Mac image runs CPU inference inside the Linux container.

The GitHub Actions workflow publishes `linux/amd64` and `linux/arm64` images to `ghcr.io/adityamendhe7/stream-to-shorts` on pushes to `main` and tags beginning with `v`. After the package is published, pull it with:

```bash
docker pull ghcr.io/adityamendhe7/stream-to-shorts:latest
```

For a release tag such as `v1.0.0`, the corresponding image tag is published too. Set the package visibility to public in GitHub Packages if you want unauthenticated pulls.

## YouTube upload setup

Uploads use the YouTube Data API with OAuth. Follow [Google’s Python quickstart](https://developers.google.com/youtube/data/quickstart/python) to create a Google Cloud project, enable the YouTube Data API v3, and create a Desktop app OAuth client. The uploader uses the `youtube.upload` scope and the API’s `videos.insert` method ([upload guide](https://developers.google.com/youtube/data/guides/uploading_a_video)).

Save the downloaded OAuth client file locally as `secrets/client_secret.json`. Authenticate once from the host so the browser-based desktop flow can complete and save the token:

```bash
mkdir -p secrets
YOUTUBE_CLIENT_SECRET_PATH="$PWD/secrets/client_secret.json" \
YOUTUBE_TOKEN_PATH="$PWD/secrets/token.json" \
python clipper.py gameplay.mp4 --upload-youtube
```

The default upload privacy is `private`. Review the video in YouTube Studio before changing it with `--privacy unlisted` or `--privacy public`.

For Docker uploads, mount the already-authorized files at runtime. Do not copy them into the image:

```bash
docker run --rm \
  -v "$PWD/input:/data/input:ro" \
  -v "$PWD/clips:/data/clips" \
  -v "$PWD/models:/data/models" \
  -v "$PWD/secrets:/run/secrets" \
  -e YOUTUBE_CLIENT_SECRET_PATH=/run/secrets/client_secret.json \
  -e YOUTUBE_TOKEN_PATH=/run/secrets/token.json \
  stream-to-shorts:local /data/input/gameplay.mp4 --upload-youtube
```

OAuth files, environment files, downloads, clips, live sessions, and video files are excluded by `.gitignore` and `.dockerignore`.

## Live recording

Watch a supported live URL, save a session, and cut clips when the stream ends:

```bash
python live.py "LIVE_STREAM_URL" --auto-clip --visual-scan --count 5
```

To cut a saved session later:

```bash
python live.py --cut live/SESSION_FOLDER --visual-scan --count 5
```

Live audio, speech, and chat detection runs during capture. The visual scan runs after the recording has been saved. YouTube live chat support depends on the stream and chat downloader availability; Twitch chat is read anonymously when available.

## Evaluation tooling

Create a CSV of hand-labeled event peak timestamps. Use a `video` column containing each source filename and a `timestamp` column in seconds, `MM:SS`, or `HH:MM:SS`. Add one row per labeled event. This repository does not include evaluation labels or results.

For each source video, write an evaluation ranking file while analyzing it:

```bash
python clipper.py gameplay.mp4 --visual-scan --evaluation-json evaluation/gameplay.json
```

Add `--gemini-review` to include an opt-in Gemini ranking. Then calculate precision@k:

```bash
python evaluate.py --labels evaluation/labels.csv \
  --rankings evaluation/gameplay.json -k 5 --tolerance 5
```

Pass multiple `--rankings` files to aggregate several videos. A prediction is relevant when its peak is within the tolerance of one unused labeled timestamp. The script reports micro precision@k for audio/transcript, chat, visual, merged, and Gemini rankings. Gemini is reported as unavailable when no reviewed scores were produced.

## Limits

- The visual detector measures frame changes; it does not recognize GTA-specific events such as explosions, crashes, wins, or streamer facial expressions. Camera cuts, flashes, and ordinary movement can still trigger it. Audio energy and short-window suppression reduce some false triggers but do not establish that a moment is entertaining.
- The built-in transcript scorer uses a fixed reaction vocabulary. It can miss subtle reactions, unfamiliar slang, and excitement expressed without speech.
- Clip framing is rule-based. `fit` preserves the whole frame but can make gameplay small; `stack` needs a manually supplied facecam rectangle; `crop` can cut off the streamer or gameplay if `--crop-x` is not adjusted. There is no automatic face tracking.
- Chat replay may be unavailable, incomplete, delayed, or unsupported for a source. Download sites can change and `yt-dlp` support may vary.
- Whisper transcription and video rendering can take time and use substantial CPU, memory, and disk space. Cached analysis is keyed by the source video’s SHA-256, so changing the video creates a new cache.
- Gemini is optional, may incur API usage, and scores only candidates found by local detectors. It cannot recover a moment the local candidate generation missed.
- Local preview requires the source video and candidate file to remain available while the review server is running. The server binds to `127.0.0.1` and is intended for local use.

## Development

Install the development extra in the virtual environment:

```bash
python -m pip install -e '.[dev]'
```

Run the synthetic unit tests and lint check:

```bash
python -m pytest
ruff check .
```

The tests use synthetic signals and mocked Gemini responses. They do not download videos, contact Gemini, or use evaluation labels. GitHub Actions runs the same lint and test checks on pushes to `main` and pull requests.
