import json

import pytest

import clipper
import preview


def test_preview_saves_only_selected_candidates_with_content_hash(tmp_path):
    video = tmp_path / "session.mp4"
    video.write_bytes(b"recording-bytes")
    candidates = [
        {"source": "session.mp4", "start": 1, "end": 8, "peak": 4, "rank": 2},
        {"source": "session.mp4", "start": 12, "end": 20, "peak": 16, "rank": 1},
    ]
    output = tmp_path / "approved.json"

    manifest = preview.save_approved_candidates(candidates, [1], video, output)

    assert manifest["source"] == video.name
    assert manifest["source_sha256"] == clipper.video_sha256(video)
    assert manifest["candidates"] == [candidates[1]]
    assert output.exists()
    assert clipper.load_approved_candidates(
        output, video, manifest["source_sha256"], 25
    ) == [candidates[1]]


def test_preview_rejects_empty_or_invalid_selection(tmp_path):
    video = tmp_path / "session.mp4"
    video.write_bytes(b"recording-bytes")
    candidates = [{"peak": 3}]

    with pytest.raises(ValueError, match="approve at least one"):
        preview.build_approval_manifest(candidates, [], video)
    with pytest.raises(ValueError, match="invalid candidate index"):
        preview.build_approval_manifest(candidates, [1], video)


def test_export_rejects_manifest_for_changed_source(tmp_path):
    video = tmp_path / "session.mp4"
    video.write_bytes(b"recording-bytes")
    candidate = {"start": 1, "end": 8, "peak": 4, "rank": 2}
    manifest = preview.build_approval_manifest([candidate], [0], video)
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(manifest))
    video.write_bytes(b"changed-recording")

    with pytest.raises(SystemExit, match="different or changed source video"):
        clipper.load_approved_candidates(path, video, clipper.video_sha256(video), 20)


