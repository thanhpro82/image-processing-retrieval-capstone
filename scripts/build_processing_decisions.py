"""Run the conservative post-crawl decision gate on a local image manifest.

The gate never infers entity relevance or safety from a human-approved corpus.
It checks each distinct file once, reuses byte-identical assets, and routes
perceptual matches to review rather than deleting them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT / "data" / "corpus_v1"


def rows_from(path: Path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def inspect_file(path: Path, expected_dimensions: list[int] | None) -> dict:
    """Return a technical result; a failed source is quarantined, not rejected for content."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        with Image.open(path) as image:
            image.load()
            dimensions = list(image.size)
            if image.format != "WEBP":
                return {"status": "invalid_format", "sha256": digest.hexdigest()}
        if expected_dimensions and dimensions != expected_dimensions:
            return {"status": "dimension_mismatch", "sha256": digest.hexdigest(),
                    "dimensions": dimensions}
        return {"status": "valid", "sha256": digest.hexdigest(),
                "dimensions": dimensions, "bytes": path.stat().st_size}
    except (OSError, ValueError) as exc:
        return {"status": "unreadable", "error": type(exc).__name__}


def near_duplicate_keys(path: Path | None) -> set[str]:
    keys: set[str] = set()
    if path and path.is_file():
        for pair in rows_from(path):
            keys.add(pair["left_asset_key"])
            keys.add(pair["right_asset_key"])
    return keys


def make_decision(row: dict, technical: dict | None, first_by_hash: dict[str, int],
                  near_keys: set[str], ordinal: int) -> dict:
    status = row.get("status")
    result = {
        "row": ordinal,
        "source_url": row.get("source_url"),
        "image_path": row.get("image_path"),
        "human_label": row.get("human_label"),
        "source_status": status,
    }
    if status != "valid_image":
        result.update(action="retry_or_review_source", reason="source_not_available")
        return result
    if technical is None or technical["status"] != "valid":
        result.update(action="quarantine_file", reason=(technical or {}).get("status", "missing_file"))
        return result
    sha = technical["sha256"]
    if row.get("processed_sha256") and row["processed_sha256"] != sha:
        result.update(action="quarantine_file", reason="checksum_mismatch")
        return result
    if row.get("processed_dimensions") and row["processed_dimensions"] != technical["dimensions"]:
        result.update(action="quarantine_file", reason="dimension_mismatch")
        return result
    result["processed_sha256"] = sha
    if sha in first_by_hash:
        result.update(action="reuse_exact_asset", reason="same_processed_bytes",
                      first_row=first_by_hash[sha])
        return result
    first_by_hash[sha] = ordinal
    asset_key = ("processed:" + row["processed_sha256"] if row.get("processed_sha256")
                 else "source:" + row["source_sha256"] if row.get("source_sha256") else None)
    if asset_key in near_keys:
        result.update(action="review_near_duplicate", reason="unconfirmed_dhash_candidate")
    else:
        result.update(action="pass_technical_pending_content", reason="technical_checks_passed")
    return result


def run(corpus: Path, manifest: Path, near_pairs: Path | None, output: Path,
        report_path: Path, workers: int, labels_csv: Path | None = None,
        image_root: Path | None = None) -> dict:
    started = time.perf_counter()
    rows = list(rows_from(manifest))
    if labels_csv:
        with labels_csv.open(encoding="utf-8-sig", newline="") as source:
            labels = {(item["batch"], item["entity_id"], item["source_url"]):
                      item["human_label"] for item in csv.DictReader(source)}
        for row in rows:
            key = (str(row.get("batch")), str(row.get("entity_id")), row.get("source_url"))
            if key in labels:
                row["human_label"] = labels[key]
    paths: dict[str, list[int] | None] = {}
    for row in rows:
        if row.get("status") == "valid_image" and row.get("image_path"):
            paths.setdefault(row["image_path"], row.get("processed_dimensions"))
    # One decode/hash per stored path. Keep the original manifest row order in
    # the output so that runs are deterministic despite concurrent inspection.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        inspected = dict(zip(paths, pool.map(
            lambda item: inspect_file((image_root or corpus) / item[0], item[1]), paths.items()), strict=True))
    first_by_hash: dict[str, int] = {}
    near_keys = near_duplicate_keys(near_pairs)
    counts: Counter[str] = Counter()
    human_labels: Counter[str] = Counter()
    action_by_label: dict[str, Counter[str]] = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as sink:
        for ordinal, row in enumerate(rows, 1):
            decision = make_decision(row, inspected.get(row.get("image_path")),
                                     first_by_hash, near_keys, ordinal)
            counts[decision["action"]] += 1
            label = str(row.get("human_label"))
            human_labels[label] += 1
            action_by_label.setdefault(label, Counter())[decision["action"]] += 1
            sink.write(json.dumps(decision, ensure_ascii=False) + "\n")
    os.replace(temporary, output)
    elapsed = time.perf_counter() - started
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest),
        "rows": len(rows),
        "distinct_paths_inspected": len(paths),
        "distinct_valid_processed_sha256": len(first_by_hash),
        "workers": workers,
        "elapsed_seconds": round(elapsed, 3),
        "manifest_rows_per_second": round(len(rows) / elapsed, 2),
        "action_counts": dict(counts),
        "human_label_counts": dict(human_labels),
        "action_by_human_label": {label: dict(actions) for label, actions in action_by_label.items()},
        "decision_file": str(output),
        "semantics": {
            "pass_technical_pending_content": "Integrity passed; entity, watermark and safety unverified by automation.",
            "reuse_exact_asset": "One physical processed asset is reused; entity/source links stay in the manifest.",
            "review_near_duplicate": "dHash candidate only; no automatic near-duplicate deletion.",
            "retry_or_review_source": "Unavailable source is not a bad-content label.",
            "quarantine_file": "Missing, corrupt or mismatched local file; investigate or reacquire.",
        },
        "limitations": [
            "The input corpus was selected using human keep labels. This run does not measure automatic semantic acceptance.",
            "Near-duplicate lookup is from an existing candidate file and does not measure candidate generation time.",
            "This local implementation uses memory proportional to corpus rows and is not itself a 100M-image deployment.",
            "Reported throughput includes local file checks and decision export, not network crawl, model inference or human review.",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--image-root", type=Path,
                        help="Directory containing image_path entries; defaults to --corpus")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--near-pairs", type=Path)
    parser.add_argument("--labels-csv", type=Path,
                        help="Optional human labels joined by batch, entity_id and URL; pilot evaluation only")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    corpus = args.corpus.resolve()
    report = run(
        corpus, args.manifest or corpus / "assembled_manifest.jsonl",
        args.near_pairs or corpus / "near_duplicate_candidates.jsonl",
        args.output or corpus / "processing_decisions.jsonl",
        args.report or corpus / "processing_decisions_report.json", args.workers,
        args.labels_csv, args.image_root,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
