"""Prepare a blinded, stratified reason-specific validation pilot.

Human keep/reject and note-derived strata stay in an ignored provenance file.
Reviewer sheets expose only the image and entity context; they do not become
ground truth until independent judgments are collected and adjudicated.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TARGETS = {
    "watermark_or_text_overlay": 20,
    "wrong_entity_or_location": 59,
    "low_resolution_or_image_quality": 5,
    "other_reject": 40,
    "keep": 100,
}
REVIEW_FIELDS = (
    "case_id", "batch", "entity_name", "entity_description", "image_file",
    "matches_entity", "watermark_or_obstructive_text", "low_quality",
    "adult_or_sensitive_content", "uncertain_or_unreadable", "review_note",
)


def stratum(row: dict[str, str]) -> str:
    if row["human_label"] == "keep":
        return "keep"
    reason = row["normalized_reason"]
    return reason if reason in TARGETS else "other_reject"


def case_id(row: dict[str, str]) -> str:
    key = "\x1f".join((row["batch"], row["entity_id"], row["source_url"]))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def sample_rows(rows: list[dict[str, str]], seed: int,
                targets: dict[str, int] = TARGETS) -> tuple[list[dict[str, str]], dict]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: set[str] = set()
    for row in rows:
        if row["image_status"] != "valid_image" or row["human_label"] not in {"keep", "reject"}:
            continue
        identifier = case_id(row)
        if identifier in seen:
            continue
        seen.add(identifier)
        groups[stratum(row)].append(row)
    rng = random.Random(seed)
    picked: list[dict[str, str]] = []
    strata = {}
    for name, target in targets.items():
        population = sorted(groups[name], key=case_id)
        chosen = rng.sample(population, min(target, len(population)))
        picked.extend(chosen)
        strata[name] = {
            "available": len(population), "selected": len(chosen),
            "inclusion_probability": round(len(chosen) / len(population), 6) if population else None,
        }
    rng.shuffle(picked)
    return picked, strata


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path,
                        default=PROJECT / "data/analysis/processing_pilot_label_audit.csv")
    parser.add_argument("--output-dir", type=Path,
                        default=PROJECT / "data/analysis/reason_validation_pilot")
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    with args.audit.open(encoding="utf-8-sig", newline="") as handle:
        source = list(csv.DictReader(handle))
    selected, strata = sample_rows(source, args.seed)
    if not selected:
        raise ValueError("No valid labeled images were available")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    blind = []
    provenance = []
    for row in selected:
        blind.append({
            "case_id": case_id(row), "batch": row["batch"],
            "entity_name": row["entity_name"],
            "entity_description": row["entity_description"],
            "image_file": row["image_file"],
        })
        provenance.append({
            "case_id": case_id(row), "batch": row["batch"],
            "entity_id": row["entity_id"], "source_url": row["source_url"],
            "human_label": row["human_label"], "original_note": row["original_note"],
            "note_derived_stratum": stratum(row),
            "label_source_file": row["label_source_file"],
            "label_source_excel_row": row["label_source_excel_row"],
        })
    for reviewer, offset in (("reviewer_1", 1), ("reviewer_2", 2)):
        shuffled = blind.copy()
        random.Random(args.seed + offset).shuffle(shuffled)
        write_csv(args.output_dir / f"{reviewer}.csv", REVIEW_FIELDS, shuffled)
    write_csv(args.output_dir / "provenance_do_not_show_reviewers.csv",
              tuple(provenance[0].keys()), provenance)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed, "source_rows": len(source), "selected_cases": len(selected),
        "strata": strata,
        "selected_batches": dict(sorted(Counter(row["batch"] for row in selected).items())),
        "reviewer_rule": "For each issue use yes/no/uncertain; leave blank until judged."
                         " Keep reviewer files separate and blind to original note/decision.",
        "limitations": [
            "Note-derived strata are sampling aids, not reason-specific ground truth.",
            "This pilot over-samples rejects; raw positive fractions are not corpus prevalence.",
            "Two independent reviewer sheets do not themselves establish agreement or adjudication.",
            "The sample is from an existing human-reviewed pilot, not a fresh crawl distribution.",
        ],
    }
    (args.output_dir / "sampling_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(args.output_dir), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
