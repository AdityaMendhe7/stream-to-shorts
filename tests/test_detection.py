import json
import sys
import types

import numpy as np

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

    def fake_ffmpeg(command, **kwargs):
        output = command[-1]
        with open(output, "wb") as frame:
            frame.write(b"synthetic-frame")
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
        "unused.mp4", moments, [], tmp_path, "fake-model"
    )

    assert ranked[0]["peak"] == 35.0
    assert [
        (moment["start"], moment["end"], moment["peak"]) for moment in moments
    ] == original_timestamps


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

    def fake_ffmpeg(command, **kwargs):
        with open(command[-1], "wb") as frame:
            frame.write(b"synthetic-frame")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipper.subprocess, "run", fake_ffmpeg)
    candidates = [{"start": 1, "end": 2, "peak": 1.5, "rank": 1}]

    assert clipper.gemini_review("unused.mp4", candidates, [], tmp_path, "model") is candidates
