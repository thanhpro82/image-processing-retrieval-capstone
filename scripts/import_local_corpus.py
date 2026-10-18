"""Reuse an already-downloaded local corpus without copying company image data.

The source manifest/status are never modified. Imported WebP files are verified,
hashed, and hard-linked on the same volume. This is a local migration utility;
the final manifest records which pipeline produced each image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
DESTINATION = PROJECT / "data" / "corpus_v1"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def latest_by_url(path: Path) -> dict[str, dict]:
    return {row["source_url"]: row for row in read_jsonl(path)}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def import_one(source: Path, selected: dict, source_id: str, output: Path) -> dict:
    with Image.open(source) as image:
        if image.format != "WEBP":
            raise ValueError(f"Unexpected format: {image.format}")
        dimensions = list(image.size)
        image.verify()
    content_sha = sha256_file(source)
    destination = output / "images" / content_sha[:2] / (content_sha + ".webp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        # Never silently copy gigabytes when hard links cannot be created.
        os.link(source, destination)
    if destination.stat().st_size != source.stat().st_size:
        raise ValueError(f"Size mismatch after hard link: {source}")
    return {
        **selected,
        "status": "valid_image",
        "image_path": str(destination.relative_to(output)),
        "processed_dimensions": dimensions,
        "stored_bytes": destination.stat().st_size,
        "processed_sha256": content_sha,
        "source_sha256": None,
        "asset_origin": "local Image retrieval corpus; source URL matched canonical Team 3 selection",
        "origin_image_id": source_id,
        "origin_transform": "RGB WebP max_side=640 quality=80",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True,
                        help="Directory containing manifest.parquet, status.parquet and images/")
    parser.add_argument("--output", type=Path, default=DESTINATION)
    parser.add_argument("--limit", type=int, default=None, help="Import only this many new files for a smoke test")
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if source == output:
        raise ValueError("Source and output must differ")
    selected = read_jsonl(output / "selection.jsonl")
    selected_by_url = {row["source_url"]: row for row in selected}
    existing = latest_by_url(output / "results.jsonl")
    imported_path = output / "imported_results.jsonl"
    imported = latest_by_url(imported_path)
    manifest = pd.read_parquet(source / "manifest.parquet", columns=["image_id", "url"])
    status = pd.read_parquet(source / "status.parquet", columns=["image_id", "ok"])
    available = manifest.merge(status[status["ok"]], on="image_id", how="inner")
    available = available[available["url"].isin(selected_by_url)].drop_duplicates("url")
    candidates = []
    for row in available.itertuples(index=False):
        if existing.get(row.url, {}).get("status") == "valid_image":
            continue
        if row.url in imported and (output / imported[row.url]["image_path"]).is_file():
            continue
        image_path = source / "images" / f"{row.image_id}.webp"
        if image_path.is_file():
            candidates.append((row.url, str(row.image_id), image_path))
    print(f"selected={len(selected)} already_imported={len(imported)} new_candidates={len(candidates)}", flush=True)
    if args.limit is not None:
        candidates = candidates[:args.limit]
    started = time.perf_counter()
    errors = Counter()
    with imported_path.open("a", encoding="utf-8", newline="\n") as log:
        for i, (url, image_id, image_path) in enumerate(candidates, 1):
            try:
                result = import_one(image_path, selected_by_url[url], image_id, output)
            except (OSError, ValueError) as exc:
                errors[f"{type(exc).__name__}: {exc}"[:160]] += 1
                continue
            imported[url] = result
            log.write(json.dumps(result, ensure_ascii=False) + "\n")
            if i % 500 == 0 or i == len(candidates):
                log.flush()
                print(f"imported={i}/{len(candidates)}", flush=True)

    # Assemble one canonical row per selected URL. A verified local download takes
    # precedence; an imported file can replace a failed or unattempted download.
    assembled_path = output / "assembled_manifest.jsonl"
    temp = assembled_path.with_suffix(".jsonl.tmp")
    chosen = Counter()
    unique_paths = set()
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        for row in selected:
            url = row["source_url"]
            own = existing.get(url)
            external = imported.get(url)
            if own and own.get("status") == "valid_image" and (output / own["image_path"]).is_file():
                final = {**row, **own, "asset_origin": "capstone local downloader"}
                chosen["own_valid"] += 1
            elif external and (output / external["image_path"]).is_file():
                final = external
                chosen["imported_valid"] += 1
            elif own:
                final = {**row, **own}
                chosen["own_error"] += 1
            else:
                final = {**row, "status": "not_downloaded"}
                chosen["not_downloaded"] += 1
            if final["status"] == "valid_image":
                unique_paths.add(final["image_path"])
            stream.write(json.dumps(final, ensure_ascii=False) + "\n")
    os.replace(temp, assembled_path)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "selected_urls": len(selected),
        "assembled_counts": dict(chosen),
        "valid_image_rows": chosen["own_valid"] + chosen["imported_valid"],
        "unique_file_paths": len(unique_paths),
        "imported_this_run": len(candidates) - sum(errors.values()),
        "import_errors": dict(errors),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "notes": [
            "The two WebP pipelines use different resize/quality settings; origin is recorded per row.",
            "Imported processed SHA-256 is not a hash of the original HTTP bytes.",
            "Unique file paths are not yet proof of unique visual content; run perceptual duplicate audit.",
            "The source corpus and company data remain local and are excluded from Git.",
        ],
    }
    atomic_json(output / "import_report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
