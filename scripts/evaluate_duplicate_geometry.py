"""Evaluate geometric confirmation of dHash candidate pairs on human labels.

The result is an experiment and never deletes an asset. It uses SIFT matches
and RANSAC homography to distinguish the same photo under resize/crop from
different photos of a similar subject.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

PROJECT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def pair_key(row: dict) -> tuple[str, str]:
    return tuple(sorted((row["left_url"], row["right_url"])))


def geometry(path_a: str, path_b: str) -> dict:
    images = []
    for path in (path_a, path_b):
        image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if image is None:
            return {"keypoints_min": 0, "good_matches": 0, "inliers": 0,
                    "inlier_fraction": 0.0, "error": "unreadable"}
        h, w = image.shape
        if max(h, w) > 768:
            scale = 768 / max(h, w)
            image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
        images.append(image)
    sift = cv2.SIFT_create(nfeatures=1500)
    kp_a, des_a = sift.detectAndCompute(images[0], None)
    kp_b, des_b = sift.detectAndCompute(images[1], None)
    minimum = min(len(kp_a), len(kp_b))
    if des_a is None or des_b is None or minimum < 4:
        return {"keypoints_min": minimum, "good_matches": 0, "inliers": 0,
                "inlier_fraction": 0.0}
    matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(des_a, des_b, k=2)
    good = [first for pair in matches if len(pair) == 2
            for first, second in [pair] if first.distance < 0.75 * second.distance]
    if len(good) < 4:
        return {"keypoints_min": minimum, "good_matches": len(good),
                "inliers": 0, "inlier_fraction": 0.0}
    source = np.float32([kp_a[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    target = np.float32([kp_b[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    try:
        _, mask = cv2.findHomography(source, target, cv2.RANSAC, 4.0)
    except cv2.error:
        mask = None
    inliers = int(mask.sum()) if mask is not None else 0
    return {"keypoints_min": minimum, "good_matches": len(good),
            "inliers": inliers, "inlier_fraction": round(inliers / len(good), 4)}


def group_split(rows: list[dict], seed: int) -> np.ndarray:
    """Put pairs sharing any URL on the same side of a deterministic split."""
    parent: dict[str, str] = {}

    def root(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    for row in rows:
        a, b = row["left_url"], row["right_url"]
        parent[root(b)] = root(a)
    groups = sorted({root(row["left_url"]) for row in rows})
    np.random.default_rng(seed).shuffle(groups)
    calibration_groups = set(groups[:len(groups) // 2])
    return np.asarray([root(row["left_url"]) in calibration_groups for row in rows])


def metrics(actual: np.ndarray, selected: np.ndarray) -> dict:
    tp = int(np.sum(actual & selected))
    fp = int(np.sum(~actual & selected))
    return {
        "pairs": len(actual), "same_photo": int(actual.sum()),
        "different_or_similar_photo": int((~actual).sum()),
        "flagged_same_photo_candidate": int(selected.sum()),
        "correct_flags": tp, "incorrect_flags": fp,
        "same_photo_recall": round(tp / int(actual.sum()), 4) if actual.any() else None,
        "flag_precision": round(tp / int(selected.sum()), 4) if selected.any() else None,
        "other_photo_false_flag_rate": round(fp / int((~actual).sum()), 4) if (~actual).any() else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, default=PROJECT / "data/corpus_v1/near_duplicate_candidates.jsonl")
    parser.add_argument("--labels", type=Path, default=PROJECT / "data/analysis/near_duplicate_review_labels.jsonl")
    parser.add_argument("--output", type=Path, default=PROJECT / "data/analysis/duplicate_geometry_report.json")
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    cv2.setRNGSeed(args.seed)
    cv2.setNumThreads(1)
    candidates = {pair_key(row): row for row in read_jsonl(args.candidates)}
    labeled = [row for row in read_jsonl(args.labels) if row.get("label") in
               {"duplicate", "similar_not_duplicate", "different"}]
    if len(labeled) < 50:
        raise ValueError("At least 50 labeled candidate pairs are needed for this pilot")
    missing = [row["sample_id"] for row in labeled if pair_key(row) not in candidates]
    if missing:
        raise ValueError(f"Labeled candidate pairs not in source file: {missing[:10]}")
    started = time.perf_counter()
    features = [geometry(candidates[pair_key(row)]["left_path"],
                         candidates[pair_key(row)]["right_path"]) for row in labeled]
    elapsed = time.perf_counter() - started
    actual = np.asarray([row["label"] == "duplicate" for row in labeled])
    calibration = group_split(labeled, args.seed)
    test = ~calibration
    inliers = np.asarray([feature["inliers"] for feature in features])
    fractions = np.asarray([feature["inlier_fraction"] for feature in features])
    # Threshold chosen only on calibration; require zero observed false flags.
    options = [(999999, 1.0)]
    options += [(n, share) for n in (8, 12, 16, 24, 32, 48)
                for share in (0.4, 0.5, 0.6, 0.7, 0.8)]
    eligible = []
    for n, share in options:
        flagged = (inliers >= n) & (fractions >= share)
        scored = metrics(actual[calibration], flagged[calibration])
        if scored["incorrect_flags"] == 0:
            eligible.append((scored["correct_flags"], -int(flagged[calibration].sum()), n, share))
    _, _, selected_n, selected_share = max(eligible)
    flagged = (inliers >= selected_n) & (fractions >= selected_share)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "SIFT Lowe-ratio matches then RANSAC homography; dHash<=4 candidate stage supplied externally",
        "opencv_version": cv2.__version__, "max_image_side_px": 768, "workers": 1,
        "seed": args.seed, "labeled_pairs": len(labeled),
        "label_counts": dict(Counter(row["label"] for row in labeled)),
        "feature_seconds": round(elapsed, 3),
        "feature_pairs_per_second": round(len(labeled) / elapsed, 2) if elapsed else None,
        "group_split": "URL-connected components; no sampled URL appears on both sides",
        "selected_min_inliers": None if selected_n == 999999 else selected_n,
        "selected_min_inlier_fraction": None if selected_n == 999999 else selected_share,
        "calibration": metrics(actual[calibration], flagged[calibration]),
        "test": metrics(actual[test], flagged[test]),
        "baseline_flag_none_test": metrics(actual[test], np.zeros(int(test.sum()), dtype=bool)),
        "test_false_flag_sample_ids": [row["sample_id"] for row, is_test, truth, predicted
                                        in zip(labeled, test, actual, flagged, strict=True)
                                        if is_test and predicted and not truth],
        "test_missed_duplicate_sample_ids": [row["sample_id"] for row, is_test, truth, predicted
                                             in zip(labeled, test, actual, flagged, strict=True)
                                             if is_test and truth and not predicted],
        "errors": dict(Counter(feature.get("error", "none") for feature in features)),
        "limitations": [
            "Only one reviewer labeled this 100-pair stratified sample; it is not representative without weighting.",
            "Zero calibration false flags does not establish a sufficiently low production false-merge bound.",
            "This experiment does not measure dHash generation or all 9k candidate pairs.",
            "No candidate is automatically deleted; matching image content can still have distinct entity links.",
            "SIFT can miss low-texture or strongly cropped duplicates and may match repetitive designs.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
