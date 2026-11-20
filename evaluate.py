#!/usr/bin/env python3
"""Measure saved detector rankings against hand-labeled event timestamps."""
import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


SYSTEMS = ("audio", "chat", "visual", "merged", "gemini")


def parse_timestamp(value):
    value = value.strip()
    if not value:
        raise ValueError("timestamp is empty")
    if ":" not in value:
        seconds = float(value)
    else:
        pieces = [float(piece) for piece in value.split(":")]
        if len(pieces) == 2:
            minutes, seconds = pieces
            seconds += minutes * 60
        elif len(pieces) == 3:
            hours, minutes, seconds = pieces
            seconds += minutes * 60 + hours * 3600
        else:
            raise ValueError(f"invalid timestamp {value!r}; use seconds, MM:SS, or HH:MM:SS")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"invalid nonnegative timestamp: {value!r}")
    return seconds


def load_labels(path):
    labels = defaultdict(list)
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"video", "timestamp"}.issubset(reader.fieldnames):
            raise ValueError("labels CSV needs columns named 'video' and 'timestamp'")
        for line, row in enumerate(reader, start=2):
            source = Path((row.get("video") or "").strip()).name
            try:
                timestamp = parse_timestamp(row.get("timestamp") or "")
            except ValueError as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
            if not source:
                raise ValueError(f"{path}:{line}: video is empty")
            labels[source].append(timestamp)
    if not labels:
        raise ValueError(f"No labeled timestamps found in {path}")
    return labels


def load_rankings(paths):
    rankings = {}
    for path in paths:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        source = Path(document.get("source", "")).name
        if not source:
            raise ValueError(f"{path}: ranking JSON has no source filename")
        if source in rankings:
            raise ValueError(f"duplicate rankings for {source!r}")
        data = document.get("rankings")
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a 'rankings' object")
        for system in SYSTEMS:
            candidates = data.get(system)
            if system != "gemini" and not isinstance(candidates, list):
                raise ValueError(f"{path}: missing detector ranking {system!r}")
            if system == "gemini" and candidates is not None and not isinstance(candidates, list):
                raise ValueError(f"{path}: Gemini ranking must be a list or null")
        rankings[source] = data
    return rankings


def precision_at_k(candidates, labels, k, tolerance):
    """Return (matched predictions, number of predictions evaluated)."""
    if candidates is None:
        return None
    # Ranking arrays are written in score order; Gemini's list is already
    # re-ranked by its rubric while the other lists use detector rank.
    selected = candidates[:k]
    unmatched_labels = set(range(len(labels)))
    true_positives = 0
    for candidate in selected:
        try:
            peak = float(candidate["peak"])
        except (KeyError, TypeError, ValueError):
            continue
        match = next((index for index in sorted(unmatched_labels)
                      if abs(peak - labels[index]) <= tolerance), None)
        if match is not None:
            unmatched_labels.remove(match)
            true_positives += 1
    return true_positives, len(selected)


def report(labels, rankings, k, tolerance):
    totals = {system: [0, 0] for system in SYSTEMS}
    for source, gold in sorted(labels.items()):
        data = rankings.get(source)
        if data is None:
            print(f"{source}: no rankings file found; skipped")
            continue
        print(f"{source} ({len(gold)} labeled events)")
        for system in SYSTEMS:
            result = precision_at_k(data.get(system), gold, k, tolerance)
            if result is None:
                print(f"  {system:>6} P@{k}: N/A (Gemini review not available)")
                continue
            matched, evaluated = result
            totals[system][0] += matched
            totals[system][1] += evaluated
            value = matched / evaluated if evaluated else 0.0
            print(f"  {system:>6} P@{k}: {value:.3f} ({matched}/{evaluated})")
    print(f"Aggregate micro precision@{k} (tolerance ±{tolerance:g}s)")
    for system, (matched, evaluated) in totals.items():
        if system == "gemini" and evaluated == 0:
            print("  gemini: N/A (Gemini review not available)")
        else:
            value = matched / evaluated if evaluated else 0.0
            print(f"  {system}: {value:.3f} ({matched}/{evaluated})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path,
                        help="CSV with video,timestamp columns; timestamps are event peaks")
    parser.add_argument("--rankings", required=True, nargs="+", type=Path,
                        help="one or more files written by clipper.py --evaluation-json")
    parser.add_argument("-k", type=int, default=5, help="top candidates per video (default 5)")
    parser.add_argument("--tolerance", type=float, default=5.0,
                        help="match radius around each labeled peak in seconds (default 5)")
    args = parser.parse_args()
    if args.k < 1:
        parser.error("-k must be at least 1")
    if not math.isfinite(args.tolerance) or args.tolerance < 0:
        parser.error("--tolerance must be a finite nonnegative number")
    try:
        report(load_labels(args.labels), load_rankings(args.rankings), args.k, args.tolerance)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.exit(f"Evaluation input error: {exc}")


if __name__ == "__main__":
    main()
