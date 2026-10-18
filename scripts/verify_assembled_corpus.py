"""Audit the assembled corpus before claiming the 80,000-image requirement."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_image(corpus: Path, row: dict) -> tuple[str | None, str | None, str | None]:
    path = corpus / row["image_path"]
    if not path.is_file():
        return None, None, "missing_file"
    try:
        with Image.open(path) as image:
            image.load()
            if image.format != "WEBP":
                raise ValueError("not WebP")
            dimensions = list(image.size)
        if dimensions != row.get("processed_dimensions"):
            raise ValueError("dimension mismatch")
        actual_hash = hash_file(path)
        if row.get("processed_sha256") and actual_hash != row["processed_sha256"]:
            raise ValueError("processed checksum mismatch")
    except (OSError, ValueError) as exc:
        return None, None, str(exc)[:120]
    return actual_hash, str(path), None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=PROJECT / "data" / "corpus_v1")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    corpus = args.corpus.resolve()
    manifest = corpus / "assembled_manifest.jsonl"
    selected_urls = set()
    valid_rows = 0
    unique_hashes: dict[str, str] = {}
    unique_paths = set()
    invalid = []
    by_origin = Counter()
    by_status = Counter()
    by_batch = Counter()
    started = time.perf_counter()
    line_number = 0
    with manifest.open(encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            batch = [json.loads(line) for _, line in zip(range(500), stream)]
            if not batch:
                break
            valid_batch = [(line_number + offset + 1, row) for offset, row in enumerate(batch)
                           if row["status"] == "valid_image"]
            checks = pool.map(partial(validate_image, corpus), (row for _, row in valid_batch))
            checked = dict(zip((number for number, _ in valid_batch), checks, strict=True))
            for row in batch:
                line_number += 1
                url = row["source_url"]
                if url in selected_urls:
                    invalid.append({"line": line_number, "reason": "duplicate_url"})
                selected_urls.add(url)
                by_status[row["status"]] += 1
                if row["status"] != "valid_image":
                    continue
                actual_hash, path, error = checked[line_number]
                if error:
                    invalid.append({"line": line_number, "reason": error})
                    continue
                valid_rows += 1
                unique_paths.add(path)
                unique_hashes.setdefault(actual_hash, path)
                by_origin[row.get("asset_origin", "unknown")] += 1
                by_batch[str(row.get("batch", "unknown"))] += 1
            if line_number % 1000 == 0 or line_number == len(batch):
                print(f"checked_rows={line_number} valid_so_far={valid_rows}", flush=True)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_rows": line_number,
        "unique_source_urls": len(selected_urls),
        "valid_decodable_image_rows": valid_rows,
        "unique_existing_paths": len(unique_paths),
        "unique_processed_sha256": len(unique_hashes),
        "meets_80000_unique_processed_images": len(unique_hashes) >= 80000,
        "status_counts": dict(by_status),
        "origin_counts": dict(by_origin),
        "valid_by_batch": dict(by_batch),
        "invalid_count": len(invalid),
        "invalid_first_50": invalid[:50],
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "limitations": [
            "Exact processed SHA-256 uniqueness does not remove perceptually near-duplicate photos.",
            "This verifies file integrity and decoding, not entity relevance or image rights.",
        ],
    }
    report_path = corpus / "assembled_verification_report.json"
    temp = report_path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, report_path)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
