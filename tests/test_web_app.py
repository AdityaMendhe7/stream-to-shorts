import pytest

import web_app


def test_web_options_accept_custom_short_count():
    options = web_app.read_options({"count": "12", "length": "45", "reframe": "crop"})

    assert options["count"] == 12
    assert options["length"] == 45
    assert options["reframe"] == "crop"
    assert options["visual_scan"] is True


@pytest.mark.parametrize("count", [0, 21, "not a number"])
def test_web_options_reject_invalid_short_counts(count):
    with pytest.raises(ValueError, match="Clip count"):
        web_app.read_options({"count": count})


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=example",
        "https://youtu.be/example",
        "https://www.twitch.tv/videos/123",
        "https://kick.com/example/videos/123",
    ],
)
def test_web_accepts_supported_video_hosts(url):
    assert web_app.public_video_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://youtube.com.example.invalid/watch?v=example",
        "https://example.com/video.mp4",
    ],
)
def test_web_rejects_unsupported_video_hosts(url):
    with pytest.raises(ValueError, match="YouTube, Twitch, or Kick"):
        web_app.public_video_url(url)


def test_job_steps_include_visual_scan_only_when_enabled(tmp_path):
    options = {"visual_scan": True}
    job = web_app.make_job_record("abc123abc123", tmp_path, options, source_is_url=True)

    assert [step["id"] for step in job["steps"]] == [
        "download", "audio", "visual", "transcript", "render"
    ]
    assert job["stage_code"] == "queued"

    job = web_app.make_job_record(
        "def456def456", tmp_path, {"visual_scan": False}, source_is_url=False
    )
    assert [step["id"] for step in job["steps"]] == ["audio", "transcript", "render"]
