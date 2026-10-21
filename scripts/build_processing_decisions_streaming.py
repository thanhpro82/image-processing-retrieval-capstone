"""Disk-backed, bounded-batch version of the conservative processing gate.

This is a local scale-readiness prototype. It retains exact-asset and
near-candidate state in SQLite so manifest size does not determine Python
heap size. It does not add a semantic or safety model.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

if __package__:
    from scripts.build_processing_decisions import (
        DEFAULT_CORPUS, inspect_file, make_decision, rows_from,
    )
else:
    from build_processing_decisions import (  # type: ignore[no-redef]
        DEFAULT_CORPUS, inspect_file, make_decision, rows_from,
    )


class DiskHashes:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __contains__(self, digest: str) -> bool:
        return self.db.execute("SELECT 1 FROM hashes WHERE digest=?", (digest,)).fetchone() is not None

    def __getitem__(self, digest: str) -> int:
        row = self.db.execute("SELECT first_row FROM hashes WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise KeyError(digest)
        return int(row[0])

    def __setitem__(self, digest: str, ordinal: int) -> None:
        self.db.execute("INSERT INTO hashes(digest, first_row) VALUES (?, ?)", (digest, ordinal))


class DiskNearKeys:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __contains__(self, key: str | None) -> bool:
        return key is not None and self.db.execute(
            "SELECT 1 FROM near_keys WHERE asset_key=?", (key,)
        ).fetchone() is not None


def chunks(iterator, size: int):
    while batch := list(islice(iterator, size)):
        yield batch


def run(corpus: Path, manifest: Path, near_pairs: Path | None, output: Path,
        report_path: Path, workers: int, chunk_rows: int = 1000,
        labels_csv: Path | None = None, image_root: Path | None = None) -> dict:
    if workers < 1 or chunk_rows < 1:
        raise ValueError("workers and chunk_rows must be positive")
    started = time.perf_counter()
    labels = {}
    if labels_csv:
        with labels_csv.open(encoding="utf-8-sig", newline="") as source:
            labels = {(item["batch"], item["entity_id"], item["source_url"]):
                      item["human_label"] for item in csv.DictReader(source)}
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    decisions_tmp = output.with_suffix(output.suffix + ".tmp")
    report_tmp = report_path.with_suffix(report_path.suffix + ".tmp")
    counts: Counter[str] = Counter()
    human_labels: Counter[str] = Counter()
    action_by_label: dict[str, Counter[str]] = {}
    rows_count = 0
    with tempfile.TemporaryDirectory(prefix="processing_state_", dir=output.parent) as state_folder:
        db = sqlite3.connect(Path(state_folder) / "state.sqlite")
        try:
            db.execute("CREATE TABLE paths(path TEXT PRIMARY KEY, technical TEXT NOT NULL)")
            db.execute("CREATE TABLE hashes(digest TEXT PRIMARY KEY, first_row INTEGER NOT NULL)")
            db.execute("CREATE TABLE near_keys(asset_key TEXT PRIMARY KEY)")
            if near_pairs and near_pairs.is_file():
                for pair_batch in chunks(rows_from(near_pairs), chunk_rows):
                    db.executemany("INSERT OR IGNORE INTO near_keys(asset_key) VALUES (?)", (
                        (key,) for pair in pair_batch
                        for key in (pair["left_asset_key"], pair["right_asset_key"])
                    ))
                db.commit()
            hashes = DiskHashes(db)
            near_keys = DiskNearKeys(db)
            with ThreadPoolExecutor(max_workers=workers) as pool, decisions_tmp.open(
                "w", encoding="utf-8", newline="\n"
            ) as sink:
                for batch in chunks(rows_from(manifest), chunk_rows):
                    missing: dict[str, None] = {}
                    for row in batch:
                        path = row.get("image_path")
                        if row.get("status") == "valid_image" and path and db.execute(
                            "SELECT 1 FROM paths WHERE path=?", (path,)
                        ).fetchone() is None:
                            missing[path] = None
                    paths = list(missing)
                    # Decode only once per stored path. Row dimensions are
                    # checked afterward by make_decision, independently.
                    inspected = pool.map(
                        lambda path: inspect_file((image_root or corpus) / path, None), paths
                    )
                    db.executemany("INSERT INTO paths(path, technical) VALUES (?, ?)", (
                        (path, json.dumps(technical))
                        for path, technical in zip(paths, inspected, strict=True)
                    ))
                    for row in batch:
                        rows_count += 1
                        if labels:
                            key = (str(row.get("batch")), str(row.get("entity_id")), row.get("source_url"))
                            if key in labels:
                                row["human_label"] = labels[key]
                        path = row.get("image_path")
                        cached = db.execute("SELECT technical FROM paths WHERE path=?", (path,)).fetchone() if path else None
                        technical = json.loads(cached[0]) if cached else None
                        decision = make_decision(row, technical, hashes, near_keys, rows_count)
                        counts[decision["action"]] += 1
                        label = str(row.get("human_label"))
                        human_labels[label] += 1
                        action_by_label.setdefault(label, Counter())[decision["action"]] += 1
                        sink.write(json.dumps(decision, ensure_ascii=False) + "\n")
                    db.commit()
            path_count = db.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
            hash_count = db.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
        finally:
            db.close()
    os.replace(decisions_tmp, output)
    elapsed = time.perf_counter() - started
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest), "rows": rows_count,
        "distinct_paths_inspected": path_count,
        "distinct_valid_processed_sha256": hash_count,
        "workers": workers, "chunk_rows": chunk_rows,
        "execution_mode": "disk_backed_streaming",
        "elapsed_seconds": round(elapsed, 3),
        "manifest_rows_per_second": round(rows_count / elapsed, 2) if elapsed else None,
        "action_counts": dict(counts),
        "human_label_counts": dict(human_labels),
        "action_by_human_label": {label: dict(actions) for label, actions in action_by_label.items()},
        "decision_file": str(output),
        "limitations": [
            "Content relevance, watermark and safety remain unverified by this gate.",
            "Near-duplicate pair generation is external and excluded from throughput.",
            "SQLite state grows with unique paths, hashes and candidate keys on disk; only Python heap is batch-bounded.",
            "No 10-100M benchmark or distributed production run has been performed.",
            "Throughput excludes network crawl, model inference and human review.",
        ],
    }
    report_tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(report_tmp, report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--near-pairs", type=Path)
    parser.add_argument("--labels-csv", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-rows", type=int, default=1000)
    args = parser.parse_args()
    corpus = args.corpus.resolve()
    report = run(corpus, args.manifest or corpus / "assembled_manifest.jsonl",
                 args.near_pairs or corpus / "near_duplicate_candidates.jsonl",
                 args.output or corpus / "processing_decisions_streaming.jsonl",
                 args.report or corpus / "processing_decisions_streaming_report.json",
                 args.workers, args.chunk_rows, args.labels_csv, args.image_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
