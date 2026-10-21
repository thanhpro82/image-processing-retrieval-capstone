"""Evaluate low-cost pixel alignment on human-labeled dHash candidates.

Thresholds use a URL-disjoint calibration split. This experiment never merges
or removes images and does not estimate corpus-wide duplicate prevalence.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

if __package__:
    from scripts.evaluate_duplicate_geometry import group_split, metrics, pair_key, read_jsonl
else:
    from evaluate_duplicate_geometry import group_split, metrics, pair_key, read_jsonl

PROJECT = Path(__file__).resolve().parents[1]


def pixel_scores(path_a: str, path_b: str, side: int = 256) -> dict[str, float]:
    images = []
    for path in (path_a, path_b):
        image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"Unreadable image: {path}")
        images.append(cv2.resize(image, (side, side), interpolation=cv2.INTER_AREA).astype(np.float32))
    a, b = images
    ncc = float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0])
    if not np.isfinite(ncc):
        ncc = -1.0
    # Local SSIM as defined by Wang et al., with a fixed 11x11 Gaussian window.
    mu_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu_b = cv2.GaussianBlur(b, (11, 11), 1.5)
    var_a = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a * mu_a
    var_b = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b * mu_b
    covariance = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_a * mu_b
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    numerator = (2 * mu_a * mu_b + c1) * (2 * covariance + c2)
    denominator = (mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2)
    ssim = float(np.mean(numerator / np.maximum(denominator, 1e-6)))
    return {"ncc": ncc, "ssim": ssim}


def choose_threshold(actual: np.ndarray, calibration: np.ndarray,
                     scores: dict[str, np.ndarray]) -> tuple[str, float]:
    best = (0, 0, "none", float("inf"))
    for name, values in scores.items():
        for threshold in np.unique(values[calibration]):
            selected = values >= threshold
            result = metrics(actual[calibration], selected[calibration])
            if result["incorrect_flags"] == 0:
                candidate = (result["correct_flags"], -result["flagged_same_photo_candidate"],
                             name, float(threshold))
                if candidate[:2] > best[:2]:
                    best = candidate
    return best[2], best[3]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=PROJECT / "data/corpus_v1/near_duplicate_candidates.jsonl")
    parser.add_argument("--labels", type=Path, default=PROJECT / "data/analysis/near_duplicate_review_labels.jsonl")
    parser.add_argument("--output", type=Path, default=PROJECT / "data/analysis/duplicate_pixels_report.json")
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    candidates = {pair_key(row): row for row in read_jsonl(args.candidates)}
    labeled = [row for row in read_jsonl(args.labels) if row.get("label") in
               {"duplicate", "similar_not_duplicate", "different"}]
    if len(labeled) < 50:
        raise ValueError("At least 50 labeled candidate pairs are needed")
    missing = [row["sample_id"] for row in labeled if pair_key(row) not in candidates]
    if missing:
        raise ValueError(f"Labeled pairs not in candidate source: {missing[:10]}")
    start = time.perf_counter()
    measured = [pixel_scores(candidates[pair_key(row)]["left_path"],
                             candidates[pair_key(row)]["right_path"]) for row in labeled]
    elapsed = time.perf_counter() - start
    scores = {name: np.asarray([item[name] for item in measured]) for name in ("ncc", "ssim")}
    actual = np.asarray([row["label"] == "duplicate" for row in labeled], dtype=bool)
    calibration = group_split(labeled, args.seed)
    test = ~calibration
    if not calibration.any() or not test.any():
        raise ValueError("Need both calibration and test pairs")
    metric, threshold = choose_threshold(actual, calibration, scores)
    selected = scores[metric] >= threshold if metric != "none" else np.zeros(len(labeled), dtype=bool)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "256px grayscale pixel NCC and local SSIM on dHash<=4 candidates",
        "seed": args.seed, "labeled_pairs": len(labeled),
        "feature_seconds": round(elapsed, 3),
        "feature_pairs_per_second": round(len(labeled) / elapsed, 2) if elapsed else None,
        "group_split": "URL-connected components; no sampled URL appears on both sides",
        "selected_metric": metric,
        "selected_threshold": threshold if metric != "none" else None,
        "calibration": metrics(actual[calibration], selected[calibration]),
        "test": metrics(actual[test], selected[test]),
        "test_false_flag_sample_ids": [row["sample_id"] for row, is_test, truth, flagged
                                        in zip(labeled, test, actual, selected, strict=True)
                                        if is_test and flagged and not truth],
        "limitations": [
            "Only one reviewer labeled a stratified 100-pair sample; test precision is not corpus precision.",
            "Zero observed false flags on calibration does not prove a safe production false-merge rate.",
            "Pixel alignment can miss crops, borders, rotation or major edits.",
            "Feature speed excludes dHash candidate generation and image download.",
            "This experiment never automatically merges or deletes images or entity links.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("selected_metric", "selected_threshold",
                                               "feature_pairs_per_second", "calibration", "test")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
