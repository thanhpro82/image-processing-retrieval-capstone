"""Measure cheap technical gates on the labeled, decoded pilot.

The human reject label combines several causes. These features are exploratory;
they must not be presented as watermark, safety, or entity-relevance detectors.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

PROJECT = Path(__file__).resolve().parents[1]


def labeled_rows(pilot: Path, database: Path) -> list[dict]:
    with sqlite3.connect(database) as connection:
        labels = {
            (int(batch), str(entity_id), url): decision == ""
            for batch, entity_id, url, decision in connection.execute(
                "SELECT batch, entity_id, url, decision FROM canonical_role "
                "WHERE sheet_role='reviewed' AND decision IN ('', 'x')"
            )
        }
    rows = []
    with (pilot / "indexed_manifest.jsonl").open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            key = (int(row["batch"]), str(row["entity_id"]), row["source_url"])
            if key in labels and (pilot / "images" / row["image_path"]).is_file():
                rows.append({**row, "keep": labels[key]})
    return rows


def features(path: Path) -> dict[str, float]:
    with Image.open(path) as image:
        gray = image.convert("L")
        gray.thumbnail((256, 256))
        pixels = np.asarray(gray, dtype=np.uint8)
        hist = np.bincount(pixels.ravel(), minlength=256).astype(np.float64)
        probabilities = hist[hist > 0] / hist.sum()
        entropy = float(-(probabilities * np.log2(probabilities)).sum())
        edges = np.asarray(gray.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    return {
        "short_side_px": float(min(image.size)),
        "grayscale_entropy_bits": entropy,
        "edge_mean": float(edges.mean()),
    }


def counts(actual_keep: np.ndarray, reject: np.ndarray) -> dict[str, float | int]:
    bad = ~actual_keep
    true_reject = int(np.sum(bad & reject))
    false_reject = int(np.sum(actual_keep & reject))
    total_bad = int(bad.sum())
    total_keep = int(actual_keep.sum())
    return {
        "n": len(actual_keep),
        "human_keep": total_keep,
        "human_reject": total_bad,
        "auto_reject": int(reject.sum()),
        "true_reject": true_reject,
        "false_reject_good": false_reject,
        "reject_recall": round(true_reject / total_bad, 4) if total_bad else 0.0,
        "good_false_reject_rate": round(false_reject / total_keep, 4) if total_keep else 0.0,
        "auto_reject_precision": round(true_reject / reject.sum(), 4) if reject.any() else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=Path, default=PROJECT / "data" / "pilot")
    parser.add_argument("--database", type=Path, default=PROJECT / "data" / "analysis" / "team3_label_audit.sqlite")
    parser.add_argument("--output", type=Path, default=PROJECT / "data" / "analysis" / "technical_filter_report.json")
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    rows = labeled_rows(args.pilot, args.database)
    started = time.perf_counter()
    values = [features(args.pilot / "images" / row["image_path"]) for row in rows]
    elapsed = time.perf_counter() - started
    keep = np.array([row["keep"] for row in rows], dtype=bool)
    entity_ids = sorted({str(row["entity_id"]) for row in rows})
    np.random.default_rng(args.seed).shuffle(entity_ids)
    calibration_entities = set(entity_ids[:max(1, len(entity_ids) // 2)])
    calibration = np.array([str(row["entity_id"]) in calibration_entities for row in rows])
    test = ~calibration
    if not calibration.any() or not test.any():
        raise ValueError("Need both calibration and test entities")

    candidates = []
    for name in values[0]:
        column = np.array([value[name] for value in values])
        for threshold in np.unique(np.quantile(column[calibration], np.linspace(0, 1, 101))):
            rejected = column < threshold
            measured = counts(keep[calibration], rejected[calibration])
            if measured["good_false_reject_rate"] <= 0.01:
                candidates.append((measured["true_reject"], -measured["false_reject_good"],
                                   name, float(threshold), rejected))
    best = max(candidates, key=lambda item: item[:2])
    _, _, feature, threshold, reject = best
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Team 3 reviewed-sheet keep/x labels on valid decoded pilot images",
        "seed": args.seed,
        "images": len(rows),
        "entities": len(entity_ids),
        "feature_seconds": round(elapsed, 3),
        "feature_images_per_second": round(len(rows) / elapsed, 2),
        "selection_rule": "Maximize calibration true rejects subject to <=1% false rejection of human-kept images",
        "selected_feature": feature,
        "selected_less_than": threshold,
        "calibration": counts(keep[calibration], reject[calibration]),
        "test": counts(keep[test], reject[test]),
        "baseline_keep_all_test": counts(keep[test], np.zeros(int(test.sum()), dtype=bool)),
        "limitations": [
            "The reject label mixes wrong entity, duplicate, technical faults and other reasons.",
            "This gate does not detect watermark, NSFW, politics or entity relevance.",
            "The pilot includes only downloaded, decoded images, so download/decode rejection is measured separately.",
            "Test entities are unseen during threshold selection; this remains a small preliminary pilot.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
