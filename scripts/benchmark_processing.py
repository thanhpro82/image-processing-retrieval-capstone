"""Reproducible local throughput probe for the implemented CPU processing stages.

This is an *asset* benchmark on an already downloaded, human-reviewed corpus.
It does not measure network ingestion, semantic filtering, or safety screening.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT / "data" / "corpus_v1"


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def read_assets(manifest: Path) -> list[dict]:
    # One image path may be linked to more than one source row. Benchmark each
    # physical asset once, while retaining the reference checksum.
    assets: dict[str, dict] = {}
    with manifest.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row.get("status") == "valid_image":
                assets.setdefault(row["image_path"], row)
    return list(assets.values())


def process_one(corpus: Path, row: dict) -> dict:
    path = corpus / row["image_path"]
    started = time.perf_counter()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    checksum_ms = (time.perf_counter() - started) * 1000
    actual_hash = digest.hexdigest()
    if row.get("processed_sha256") and actual_hash != row["processed_sha256"]:
        raise ValueError(f"Checksum mismatch: {path}")

    started = time.perf_counter()
    with Image.open(path) as image:
        image.load()
        if image.format != "WEBP":
            raise ValueError(f"Unexpected image format: {path}")
        if list(image.size) != row.get("processed_dimensions"):
            raise ValueError(f"Dimension mismatch: {path}")
        gray = ImageOps.exif_transpose(image).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(gray.get_flattened_data()) if hasattr(gray, "get_flattened_data") else list(gray.getdata())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | (pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    decode_dhash_ms = (time.perf_counter() - started) * 1000
    return {
        "bytes": path.stat().st_size,
        "checksum_ms": checksum_ms,
        "decode_dhash_ms": decode_dhash_ms,
        "dhash": f"{value:016x}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--sample", type=int, default=10000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cpu-usd-per-hour", type=float,
                        help="Your actual CPU machine-hour price; omit to report hours only")
    args = parser.parse_args()
    if args.sample < 1 or args.workers < 1:
        parser.error("--sample and --workers must be positive")
    if args.cpu_usd_per_hour is not None and args.cpu_usd_per_hour < 0:
        parser.error("--cpu-usd-per-hour cannot be negative")

    corpus = args.corpus.resolve()
    all_assets = read_assets(corpus / "assembled_manifest.jsonl")
    if not all_assets:
        raise RuntimeError("No valid assets in assembled manifest")
    sampled = random.Random(args.seed).sample(all_assets, min(args.sample, len(all_assets)))
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda row: process_one(corpus, row), sampled))
    wall_seconds = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start
    total_bytes = sum(item["bytes"] for item in results)
    assets_per_second = len(results) / wall_seconds
    hours_per_million = 1_000_000 / assets_per_second / 3600
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "corpus": str(corpus),
        "manifest": "assembled_manifest.jsonl",
        "sample_method": "Seeded uniform sample of distinct valid image paths; without replacement",
        "seed": args.seed,
        "available_distinct_paths": len(all_assets),
        "sampled_assets": len(results),
        "workers": args.workers,
        "platform": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "wall_seconds": round(wall_seconds, 3),
        "process_cpu_seconds": round(cpu_seconds, 3),
        "input_bytes": total_bytes,
        "mean_bytes_per_asset": round(total_bytes / len(results), 1),
        "throughput_assets_per_second": round(assets_per_second, 2),
        "checksum_ms_p50": round(percentile([r["checksum_ms"] for r in results], 0.5), 3),
        "checksum_ms_p95": round(percentile([r["checksum_ms"] for r in results], 0.95), 3),
        "decode_dhash_ms_p50": round(percentile([r["decode_dhash_ms"] for r in results], 0.5), 3),
        "decode_dhash_ms_p95": round(percentile([r["decode_dhash_ms"] for r in results], 0.95), 3),
        "projection_same_machine_no_overheads": {
            "machine_hours_per_1m_assets": round(hours_per_million, 2),
            "machine_hours_per_10m_assets": round(hours_per_million * 10, 2),
            "machine_hours_per_100m_assets": round(hours_per_million * 100, 2),
            "cpu_compute_usd_per_1m_if_rate_supplied": (
                round(hours_per_million * args.cpu_usd_per_hour, 2)
                if args.cpu_usd_per_hour is not None else None
            ),
        },
        "limitations": [
            "Already-downloaded, human-reviewed assets; not raw crawl selection.",
            "This measures file hashing, decode, dimension check and dHash only; not candidate-pair indexing, OCR, safety or entity models.",
            "Warm filesystem caches, storage medium, concurrency and image sizes affect throughput.",
            "Projection holds measured throughput constant and excludes download, retries, storage, network, GPU, and human review costs.",
        ],
    }
    output = args.output or corpus / "processing_cpu_benchmark_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
