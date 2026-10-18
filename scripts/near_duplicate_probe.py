from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

PROJECT = Path(__file__).resolve().parents[1]
PROBE = PROJECT / "data" / "scale_probe_10k"
HASH_VERSION = "exif-dhash-9x8-lanczos-v1"
MAX_DISTANCE = 4


def dhash(path: Path) -> int:
    # Adapted from Crawl Data/tools/image_review_pipeline.py.
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("L")
        image = image.resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(image.get_flattened_data()) if hasattr(image, "get_flattened_data") else list(image.getdata())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | (pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return value


def read_latest_results() -> dict[str, dict]:
    latest = {}
    with (PROBE / "results.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                latest[row["source_url"]] = row
    return latest


def atomic_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def hash_unique(rows: list[dict], workers: int) -> list[dict]:
    cache_path = PROBE / "dhash_cache.jsonl"
    cached = {}
    if cache_path.exists():
        with cache_path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    item = json.loads(line)
                    if item.get("hash_version") == HASH_VERSION:
                        cached[item["source_sha256"]] = item["dhash"]
    pending = [row for row in rows if row["source_sha256"] not in cached]
    print(f"unique={len(rows)} cached={len(cached)} pending={len(pending)}", flush=True)
    with cache_path.open("a", encoding="utf-8", newline="\n") as log:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(dhash, PROBE / "images" / row["image_path"]): row for row in pending}
            for i, future in enumerate(as_completed(futures), 1):
                row = futures[future]
                value = future.result()
                cached[row["source_sha256"]] = f"{value:016x}"
                log.write(json.dumps({
                    "source_sha256": row["source_sha256"],
                    "hash_version": HASH_VERSION,
                    "dhash": f"{value:016x}",
                }) + "\n")
                if i % 500 == 0 or i == len(pending):
                    log.flush()
                    print(f"hashed {i}/{len(pending)}", flush=True)
    for row in rows:
        row["dhash"] = cached[row["source_sha256"]]
    return rows


def candidates(rows: list[dict]) -> list[dict]:
    buckets = defaultdict(list)
    hashes = [int(row["dhash"], 16) for row in rows]
    pairs = []
    widths = [13, 13, 13, 13, 12]
    shifts = [0, 13, 26, 39, 52]
    masks = [(1 << width) - 1 for width in widths]
    for i, value in enumerate(hashes):
        possible = set()
        for band in range(5):
            key = (band, (value >> shifts[band]) & masks[band])
            possible.update(buckets[key])
            buckets[key].append(i)
        for j in possible:
            distance = (value ^ hashes[j]).bit_count()
            if distance <= MAX_DISTANCE:
                left, right = rows[j], rows[i]
                left_entities = {(link["batch"], link["entity_id"]) for link in left["entity_links"]}
                right_entities = {(link["batch"], link["entity_id"]) for link in right["entity_links"]}
                pairs.append({
                    "left_sha256": left["source_sha256"],
                    "right_sha256": right["source_sha256"],
                    "distance": distance,
                    "same_entity": bool(left_entities & right_entities),
                    "left_path": str(PROBE / "images" / left["image_path"]),
                    "right_path": str(PROBE / "images" / right["image_path"]),
                    "left_url": left["source_url"],
                    "right_url": right["source_url"],
                })
    return pairs


def contact_sheets(pairs: list[dict], limit: int) -> list[str]:
    out = PROBE / "near_duplicate_sheets"
    out.mkdir(exist_ok=True)
    by_group = defaultdict(list)
    for pair in pairs:
        by_group[(pair["distance"], pair["same_entity"])].append(pair)
    sampled = []
    for group in sorted(by_group):
        sampled.extend(by_group[group][: max(1, limit // 10)])
    sampled = sampled[:limit]
    output_paths = []
    for start in range(0, len(sampled), 10):
        batch = sampled[start:start + 10]
        sheet = Image.new("RGB", (1020, 10 * 230), "#f8f8f8")
        draw = ImageDraw.Draw(sheet)
        for k, pair in enumerate(batch):
            y = k * 230
            draw.text((12, y + 4), f"pair {start+k+1}  dHash distance={pair['distance']}  same_entity={pair['same_entity']}", fill="#111111")
            for side, key in enumerate(("left_path", "right_path")):
                with Image.open(pair[key]) as source:
                    thumb = ImageOps.exif_transpose(source).convert("RGB")
                    thumb.thumbnail((480, 190))
                    x = 12 + side * 500 + (480 - thumb.width) // 2
                    sheet.paste(thumb, (x, y + 30))
        path = out / f"pairs_{start//10 + 1:02d}.jpg"
        sheet.save(path, quality=86)
        output_paths.append(str(path))
    return output_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--contact-pairs", type=int, default=60)
    args = parser.parse_args()
    latest = read_latest_results()
    unique = {}
    for row in latest.values():
        if row["status"] == "valid_image":
            unique.setdefault(row["source_sha256"], row)
    rows = hash_unique(list(unique.values()), args.workers)
    pairs = candidates(rows)
    pair_path = PROBE / "near_duplicate_candidates.jsonl"
    with pair_path.open("w", encoding="utf-8", newline="\n") as stream:
        for pair in pairs:
            stream.write(json.dumps(pair, ensure_ascii=False) + "\n")

    parent = list(range(len(rows)))
    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    index = {row["source_sha256"]: i for i, row in enumerate(rows)}
    for pair in pairs:
        a = find(index[pair["left_sha256"]])
        b = find(index[pair["right_sha256"]])
        parent[b] = a
    clusters = Counter(find(i) for i in range(len(rows)))
    duplicate_upper_bound = sum(n - 1 for n in clusters.values() if n > 1)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "hash_version": HASH_VERSION,
        "max_hamming_distance": MAX_DISTANCE,
        "unique_byte_images": len(rows),
        "candidate_pairs": len(pairs),
        "pairs_by_distance": dict(Counter(str(p["distance"]) for p in pairs)),
        "same_entity_candidate_pairs": sum(p["same_entity"] for p in pairs),
        "candidate_clusters": sum(n > 1 for n in clusters.values()),
        "max_images_removed_if_all_candidates_are_true_duplicates": duplicate_upper_bound,
        "candidate_files": str(pair_path),
        "contact_sheets": contact_sheets(pairs, args.contact_pairs),
        "limitations": [
            "dHash candidates are not verified duplicates; visual confirmation is required.",
            "dHash distance <=4 can miss crops, rotation, and changes in viewpoint.",
            "This probe counts exact-byte-unique images and dHash candidates only."
        ],
    }
    atomic_json(PROBE / "near_duplicate_report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("contact_sheets", "limitations", "candidate_files")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
