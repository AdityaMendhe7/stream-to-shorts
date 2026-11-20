import json

import pytest

import evaluate


def test_parse_timestamp_accepts_seconds_and_clock_formats():
    assert evaluate.parse_timestamp("83.5") == 83.5
    assert evaluate.parse_timestamp("01:23.5") == 83.5
    assert evaluate.parse_timestamp("00:01:23.5") == 83.5


def test_precision_at_k_matches_each_label_only_once():
    candidates = [
        {"peak": 10, "rank": 3},
        {"peak": 11, "rank": 2},
        {"peak": 50, "rank": 1},
    ]

    assert evaluate.precision_at_k(candidates, [10, 50], 3, 2) == (2, 3)


def test_evaluation_json_round_trip_and_missing_gemini(tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("video,timestamp\ngameplay.mp4,00:10\n")
    rankings = tmp_path / "rankings.json"
    rankings.write_text(json.dumps({
        "source": "gameplay.mp4",
        "rankings": {
            "audio": [{"peak": 10, "rank": 2}],
            "chat": [],
            "visual": [{"peak": 40, "rank": 1}],
            "merged": [{"peak": 10, "rank": 2}],
            "gemini": None,
        },
    }))

    gold = evaluate.load_labels(labels)
    data = evaluate.load_rankings([rankings])

    assert gold["gameplay.mp4"] == [10]
    assert evaluate.precision_at_k(data["gameplay.mp4"]["audio"], gold["gameplay.mp4"], 1, 5) == (1, 1)
    assert evaluate.precision_at_k(data["gameplay.mp4"]["gemini"], gold["gameplay.mp4"], 1, 5) is None


def test_labels_csv_rejects_missing_columns(tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("source,seconds\ngameplay.mp4,10\n")

    with pytest.raises(ValueError, match="video.*timestamp"):
        evaluate.load_labels(labels)
