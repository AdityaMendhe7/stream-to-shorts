# Gameplay clipper

This project finds gameplay moments using separate audio/chat/transcript signals and an optional whole-video visual-change scan. The detectors do not need to agree: either can nominate a moment. Gemini review, when enabled, ranks a bounded candidate list; it cannot create or change clip timestamps.

## Run with Docker

Docker packages the current command-line backend with FFmpeg and its Python dependencies. It does not add a web API or UI yet. Docker Desktop must be running. From the project folder:

```bash
mkdir -p input clips downloads live models secrets
docker compose build
docker compose run --rm clipper --help
```

Put a source video in `input/`, then run the clipper with its mounted container path:

```bash
docker compose run --rm clipper /data/input/gameplay.mp4 --visual-scan --count 5
```

For a URL, the downloads directory is persistent:

```bash
docker compose run --rm clipper "https://www.youtube.com/watch?v=VIDEO_ID" --visual-scan --count 5
```

For a saved live stream, session data is kept in `live/`:

```bash
docker compose run --rm --entrypoint python clipper /data/live.py "LIVE_STREAM_URL" --auto-clip --visual-scan --count 5
```

The `clips/`, `downloads/`, `live/`, and `models/` directories are mounted from the host, so generated files and Whisper model downloads persist when the container exits. The source `input/` mount is read-only. CPU inference is used in the container; Docker Desktop on Mac does not expose the Mac's Apple GPU to a Linux container.

### Optional Gemini review in Docker

Create a local `.env` file (it is ignored by Git) containing:

```dotenv
GEMINI_API_KEY=your-api-key
```

Then add `--gemini-review` to a `clipper.py` or `live.py` command. The API key is passed at runtime and is not copied into the image.

### YouTube upload credentials in Docker

To keep OAuth credentials out of the image and Git, put `client_secret.json` and a previously authorized `token.json` in the local `secrets/` folder. Authorize once using the existing non-Docker local setup, then copy the resulting token into `secrets/token.json`. The Compose setup mounts that folder at runtime. Add `--upload-youtube` to the clipper command to upload; uploads default to private unless you choose another privacy setting. Do not commit `secrets/`.

### Build and publish

Build locally with `docker compose build`. GitHub Actions also builds `linux/amd64` and `linux/arm64` images and publishes them to GitHub Container Registry on changes to the backend or Docker files on `main`. The image tags are `latest` and the commit SHA. After the first successful workflow, the package can be pulled with:

```bash
docker pull ghcr.io/adityamendhe7/stream-to-shorts:latest
```

GitHub may initially mark a newly published package private. Set the package visibility to public in the repository owner's GitHub Packages settings if you want unauthenticated pulls.

## Output framing

Rendered clips are 1080×1920 (9:16), H.264/AAC MP4. `fit` is the safe default and preserves the full input frame over a blurred background. If a separate facecam is present, `stack` can show the gameplay and facecam together; pass its rectangle as normalized `x:y:w:h` fractions. `crop` fills the portrait frame and can be shifted horizontally with `--crop-x` from 0 (left) to 1 (right).

The video title is not overlaid. The bottom handle defaults to `@gtasixviralclips`; override it with `--handle` if needed.

Examples:

```bash
# Keep the entire source visible
python clipper.py gameplay.mp4 --reframe fit --count 5

# Gameplay plus a facecam in the lower-right (coordinates are examples; adjust for your source)
python clipper.py gameplay.mp4 --reframe stack --cam 0.72:0.62:0.28:0.38 --count 5

# Fill the portrait canvas, biasing the crop to the right
python clipper.py gameplay.mp4 --reframe crop --crop-x 0.7 --count 5
```

## Independent visual candidate scan

The local visual scan samples low-resolution frames across the complete source and scores abrupt visual changes. It can nominate a candidate even when the streamer is quiet and chat is inactive. A visual change is only a clue, not proof of an exciting event; ordinary cuts, flashes, and camera movement can also trigger it. The default sampling rate is 0.5 frames/second, configurable up to 2 frames/second.

```bash
python clipper.py gameplay.mp4 --visual-scan --visual-fps 0.5 --count 5
```

Audio/chat and visual candidates are merged. Strong evidence from either source can nominate a moment; agreement gives an additional ranking bonus. Candidate details, including detector sources and scores, are written to `clips/candidates.json`.

## Optional Gemini review

Gemini can review a larger shortlist with nearby transcript text and up to three sampled JPEG frames per candidate. It scores hook, context, intensity, payoff, pacing, audio evidence, visual evidence, and transcript confidence using the configurable-in-code rubric. Missing evidence is treated as unknown. Model scores only reorder locally detected candidates; they cannot invent candidates or modify timestamps and boundaries.

This mode is opt-in. It sends transcript excerpts and sampled frames to Google; it does not upload the source video. Install the optional SDK and set an API key in the environment:

```bash
python -m pip install google-genai
export GEMINI_API_KEY="your-key"
python clipper.py gameplay.mp4 --visual-scan --gemini-review --count 5
```

The review is bounded to at most 12 candidates per run. Gemini API quotas and prices vary by model and account; check [Google AI Studio usage](https://aistudio.google.com/) and the [current Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) before enabling it. A Gemini Pro subscription does not itself include Gemini API usage. Without the SDK/key, or if the request fails, the clipper falls back to detector ranking.

## Live recordings

Live capture continues to detect moments from audio, transcript, and chat without waiting on a model call. Visual scanning happens after the recording ends, when cutting the saved session:

```bash
python live.py "LIVE_STREAM_URL" --auto-clip --visual-scan --count 5
```

Or cut a previous session:

```bash
python live.py --cut live/SESSION_FOLDER --visual-scan --count 5
```

Add `--gemini-review` to either command to enable the optional post-recording review. It is not a real-time visual detector.

## Current visual-analysis limit

The local detector measures frame-to-frame visual change; it does not itself recognize semantic events such as a GTA crash, explosion, or win. Gemini can inspect sampled frames for those events among the candidates found by the local detectors. A visually quiet event that produces no strong frame change may be missed by the local scan. Increase `--visual-fps` to inspect more frames, at the cost of longer scanning time.
