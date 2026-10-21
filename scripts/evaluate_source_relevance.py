"""Pilot a cheap source-title gate for explicit wrong-entity/location notes.

This is an offline, reason-specific experiment. It never changes processing
decisions. Human keep is the negative class; rejects with other or unclear
reasons are excluded rather than mislabeled as relevant images.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
STOPWORDS = {
    "anh", "hinh", "cua", "tai", "o", "ve", "va", "the", "mot", "di", "san",
    "le", "hoi", "khu", "dia", "diem", "du", "lich", "viet", "nam", "tinh",
    "thanh", "pho", "huyen", "xa", "phuong", "quan", "bai", "bao", "tin",
}


def tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return {word for word in re.findall(r"[a-z0-9]+", normalized)
            if len(word) > 1 and word not in STOPWORDS}


def title_coverage(entity: str, title: str) -> float:
    target = tokens(entity)
    source = tokens(title)
    return len(target & source) / len(target) if target else 0.0


def counts(actual_bad: np.ndarray, selected: np.ndarray) -> dict:
    good = ~actual_bad
    tp = int(np.sum(actual_bad & selected))
    fp = int(np.sum(good & selected))
    return {
        "images": len(actual_bad), "human_keep": int(good.sum()),
        "explicit_wrong_entity_or_location": int(actual_bad.sum()),
        "flagged_for_review": int(selected.sum()), "true_flags": tp,
        "false_flags_on_kept": fp,
        "wrong_reason_recall": round(tp / int(actual_bad.sum()), 4) if actual_bad.any() else None,
        "keep_flag_rate": round(fp / int(good.sum()), 4) if good.any() else None,
        "flag_precision_in_restricted_cohort": round(tp / int(selected.sum()), 4) if selected.any() else None,
    }


def top_fraction_flags(scores: np.ndarray, rows: list[dict], fraction: float = 0.10) -> np.ndarray:
    """Rank a bounded review queue, resolving tied scores without labels."""
    order = sorted(range(len(rows)), key=lambda i: (
        scores[i], hashlib.sha256(rows[i]["source_url"].encode("utf-8")).hexdigest(),
    ))
    selected = np.zeros(len(rows), dtype=bool)
    selected[order[:int(np.ceil(len(rows) * fraction))]] = True
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, default=PROJECT / "data/analysis/processing_pilot_label_audit.csv")
    parser.add_argument("--output", type=Path, default=PROJECT / "data/analysis/source_relevance_report.json")
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    with args.labels.open(encoding="utf-8-sig", newline="") as stream:
        records = [row for row in csv.DictReader(stream)
                   if row["image_status"] == "valid_image"
                   and (row["human_label"] == "keep" or
                        (row["human_label"] == "reject" and
                         row["normalized_reason"] == "wrong_entity_or_location"))]
    if len(records) < 100:
        raise ValueError("Too few explicit labels")
    entities = sorted({(row["batch"], row["entity_id"]) for row in records})
    np.random.default_rng(args.seed).shuffle(entities)
    calibration_ids = set(entities[:len(entities) // 2])
    calibration = np.asarray([(row["batch"], row["entity_id"]) in calibration_ids for row in records])
    test = ~calibration
    bad = np.asarray([row["human_label"] == "reject" for row in records])
    scores = np.asarray([title_coverage(row["entity_name"], row["source_title"])
                         for row in records])

    # Flag low title coverage for review. Choose threshold without test labels;
    # even a good pilot threshold must not be deployed without an external test.
    choices = [float("-inf"), *sorted(set(scores[calibration]))]
    eligible = []
    for threshold in choices:
        selected = scores[calibration] <= threshold
        measured = counts(bad[calibration], selected)
        if measured["keep_flag_rate"] is not None and measured["keep_flag_rate"] <= 0.01:
            eligible.append((measured["true_flags"], -measured["false_flags_on_kept"], threshold))
    threshold = max(eligible)[2]
    flags = scores <= threshold
    calibration_rows = [row for row, chosen in zip(records, calibration, strict=True) if chosen]
    test_rows = [row for row, chosen in zip(records, test, strict=True) if chosen]
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "Accent-folded, stopword-filtered entity token coverage in source-page title",
        "seed": args.seed,
        "cohort": "Decoded human-kept rows versus rejected rows with exactly one keyword-inferred wrong-entity/location reason",
        "excluded": "Rejects for other, mixed, missing or unclear reasons",
        "split": "50/50 by (batch, entity_id); threshold selected on calibration only",
        "threshold_low_coverage": None if threshold == float("-inf") else float(threshold),
        "calibration": counts(bad[calibration], flags[calibration]),
        "test": counts(bad[test], flags[test]),
        "keep_all_test": counts(bad[test], np.zeros(int(test.sum()), dtype=bool)),
        "review_queue_10pct_calibration": counts(
            bad[calibration], top_fraction_flags(scores[calibration], calibration_rows)),
        "review_queue_10pct_test": counts(
            bad[test], top_fraction_flags(scores[test], test_rows)),
        "limitations": [
            "The reason labels are inferred from human notes using keywords, not independently adjudicated.",
            "The cohort excludes other rejects, so its flag precision is not production precision.",
            "Source-title mismatch does not prove the image is wrong; source metadata can be generic or misleading.",
            "This score cannot screen watermark, politics, adult content or image quality.",
            "The pilot is small and drawn from human-reviewed rows, not a fresh crawl distribution.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
