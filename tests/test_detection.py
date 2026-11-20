import json
import os
import sys
import types

import numpy as np
import pytest

import clipper


def test_merge_windows_combines_overlapping_ranges():
    assert clipper.merge_windows([(10, 20), (0, 5), (4, 12), (30, 35)]) == [
        (0, 20),
        (30, 35),
    ]


def test_pick_moments_merges_independent_detector_evidence():
    loud = np.zeros(31, dtype=np.float32)
    visual = np.zeros(31, dtype=np.float32)
    loud[13:18] = 8
    visual[13:18] = 4

    moments, _ = clipper.pick_moments(
        {"loud": loud, "visual": visual}, count=1, length=2, min_score=0.1
    )

    assert moments[0]["peak"] == 15
    assert set(moments[0]["sources"]) == {"loud", "visual"}
    assert moments[0]["signals"] == 2


def test_agreement_increases_rank_over_raw_score():
    loud = np.zeros(31, dtype=np.float32)
    speech = np.zeros(31, dtype=np.float32)
    loud[13:18] = 8
    speech[13:18] = 4

    moments, _ = clipper.pick_moments(
        {"loud": loud, "speech": speech}, count=1, length=2, min_score=0.1
    )

    assert moments[0]["rank"] > moments[0]["score"]


def test_scoring_defaults_match_existing_agreement_and_rubric_values():
    assert clipper.RANKING_CONFIG["agreement_bonus"] == 0.25
    assert clipper.RANKING_CONFIG["agreement_rank_bonus"] == 0.30
    assert clipper.RUBRIC_WEIGHTS == {
        "hook": 0.20,
        "context": 0.15,
        "intensity": 0.20,
        "payoff": 0.15,
        "pacing": 0.10,
        "audio_evidence": 0.08,
        "visual_evidence": 0.08,
        "transcript_confidence": 0.04,
    }


def test_visual_audio_blend_preserves_quiet_visuals_and_suppresses_nearby_triggers():
    visual = np.zeros(20, dtype=np.float32)
    audio = np.zeros(20, dtype=np.float32)
    visual[5] = 5
    visual[6] = 4.5
    visual[10] = 5
    audio[6] = 5

    combined = clipper.combine_visual_audio_scores(visual, audio)

    assert combined[5] == 0
    assert combined[6] == pytest.approx(4.5)
    assert combined[10] == pytest.approx(3.25)
    assert np.count_nonzero(combined) == 2


def test_pick_moments_returns_candidates_in_descending_rank_order():
    speech = np.zeros(61, dtype=np.float32)
    speech[10] = 8
    speech[45] = 2

    moments, _ = clipper.pick_moments(
        {"speech": speech}, count=2, length=20, min_score=0.05
    )

    assert 8 <= moments[0]["peak"] <= 12
    assert 43 <= moments[1]["peak"] <= 47
    assert moments[0]["rank"] >= moments[1]["rank"]


def test_plan_clip_keeps_boundaries_inside_source_and_length_limits():
    score = np.zeros(20, dtype=np.float32)
    score[2:5] = [2, 5, 2]

    clip = clipper.plan_clip(
        peak=3,
        sm=score,
        total_secs=20,
        segments=[],
        max_len=10,
        min_len=6,
        pre=2,
    )

    assert 0 <= clip["start"] < clip["end"] <= 20
    assert 6 <= clip["end"] - clip["start"] <= 10


def test_cache_key_uses_video_content_not_size_or_mtime(tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"first")
    original_mtime = video.stat().st_mtime_ns
    path, cache = clipper.cache_load(video)
    cache["sentinel"] = "same-content"
    clipper.cache_save(path, cache)

    _, cached = clipper.cache_load(video)
    assert cached["sentinel"] == "same-content"

    video.write_bytes(b"other")
    os.utime(video, ns=(video.stat().st_atime_ns, original_mtime))
    _, refreshed = clipper.cache_load(video)
    assert "sentinel" not in refreshed


def test_parse_cam_rejects_invalid_or_out_of_frame_rectangles():
    for value in ("bad", "0.8:0.8:0.3:0.3", "0:0:0:0.5", "nan:0:0.2:0.2"):
        with pytest.raises(SystemExit, match="--cam must be x:y:w:h"):
            clipper.parse_cam(value)


def test_missing_ffmpeg_reports_install_hint(monkeypatch):
    monkeypatch.setattr(clipper.shutil, "which", lambda tool: None)

    with pytest.raises(SystemExit, match="Install FFmpeg"):
        clipper.need("ffmpeg")


def test_dry_run_writes_candidates_without_rendering(monkeypatch, tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"synthetic-video")
    output = tmp_path / "clips"
    loudness = np.zeros(60, dtype=np.float32)
    loudness[18:23] = 10

    monkeypatch.setattr(
        sys,
        "argv",
        ["clipper.py", str(video), "--no-transcribe", "--no-chat", "--dry-run",
         "--out", str(output), "--length", "10", "--min-length", "5", "--min-score", "0.1",
         "--evaluation-json", str(tmp_path / "evaluation.json")],
    )
    monkeypatch.setattr(clipper, "need", lambda tool: None)
    monkeypatch.setattr(clipper, "duration", lambda source: 60)
    monkeypatch.setattr(
        clipper,
        "cache_load",
        lambda source: (tmp_path / "cache.json", {"key": "synthetic", "loud": loudness.tolist()}),
    )
    monkeypatch.setattr(
        clipper,
        "render",
        lambda *args, **kwargs: pytest.fail("dry-run must not render clips"),
    )

    clipper.main()

    assert sorted(path.name for path in output.iterdir()) == ["candidates.json"]
    assert json.loads((output / "candidates.json").read_text())
    evaluation = json.loads((tmp_path / "evaluation.json").read_text())
    assert set(evaluation["rankings"]) == {"audio", "chat", "visual", "merged", "gemini"}
    assert evaluation["rankings"]["audio"]
    assert evaluation["rankings"]["gemini"] is None


def test_gemini_reranks_without_changing_candidate_timestamps(monkeypatch, tmp_path):
    class FakePart:
        @staticmethod
        def from_bytes(data, mime_type):
            return (data, mime_type)

    response_json = json.dumps(
        {
            "candidates": [
                {"candidate": 0, "scores": {"hook": 0}, "explanation": "low"},
                {"candidate": 1, "scores": {"hook": 5}, "explanation": "high"},
            ]
        }
    )

    class FakeModels:
        def generate_content(self, **kwargs):
            return types.SimpleNamespace(text=response_json)

    fake_genai = types.ModuleType("google.genai")
    fake_genai.types = types.SimpleNamespace(Part=FakePart)
    fake_genai.Client = lambda api_key: types.SimpleNamespace(models=FakeModels())
    fake_google = types.ModuleType("google")
    fake_google.__path__ = []
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    video = tmp_path / "recording.mp4"
    video.write_bytes(b"synthetic-video-content")
    extracted_frames = []

    def fake_ffmpeg(command, **kwargs):
        output = command[-1]
        with open(output, "wb") as frame:
            frame.write(b"synthetic-frame")
        extracted_frames.append(output)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipper.subprocess, "run", fake_ffmpeg)
    moments = [
        {"start": 10.0, "end": 20.0, "peak": 15.0, "rank": 9.0},
        {"start": 30.0, "end": 40.0, "peak": 35.0, "rank": 8.0},
    ]
    original_timestamps = [
        (moment["start"], moment["end"], moment["peak"]) for moment in moments
    ]

    ranked = clipper.gemini_review(
        video, moments, [], tmp_path, "fake-model"
    )

    assert ranked[0]["peak"] == 35.0
    assert [
        (moment["start"], moment["end"], moment["peak"]) for moment in moments
    ] == original_timestamps
    assert len(extracted_frames) == 6

    clipper.gemini_review(video, moments, [], tmp_path, "fake-model")
    assert len(extracted_frames) == 6


def test_gemini_falls_back_when_api_key_is_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    candidates = [{"peak": 1, "rank": 1}]

    assert clipper.gemini_review("unused.mp4", candidates, [], tmp_path, "model") is candidates


def test_gemini_falls_back_when_sdk_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    real_import = __import__

    def import_without_genai(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google" and "genai" in fromlist:
            raise ImportError("mock missing google-genai")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", import_without_genai)
    candidates = [{"peak": 1, "rank": 1}]

    assert clipper.gemini_review("unused.mp4", candidates, [], tmp_path, "model") is candidates


def test_gemini_falls_back_when_request_fails(monkeypatch, tmp_path):
    class FakePart:
        @staticmethod
        def from_bytes(data, mime_type):
            return data

    class FakeModels:
        def generate_content(self, **kwargs):
            raise RuntimeError("mock API failure")

    fake_genai = types.ModuleType("google.genai")
    fake_genai.types = types.SimpleNamespace(Part=FakePart)
    fake_genai.Client = lambda api_key: types.SimpleNamespace(models=FakeModels())
    fake_google = types.ModuleType("google")
    fake_google.__path__ = []
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    video = tmp_path / "recording.mp4"
    video.write_bytes(b"synthetic-video-content")

    def fake_ffmpeg(command, **kwargs):
        with open(command[-1], "wb") as frame:
            frame.write(b"synthetic-frame")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipper.subprocess, "run", fake_ffmpeg)
    candidates = [{"start": 1, "end": 2, "peak": 1.5, "rank": 1}]

    assert clipper.gemini_review(video, candidates, [], tmp_path, "model") is candidates
