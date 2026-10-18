from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from pilot_images import download_one

DB = PROJECT / "data" / "analysis" / "team3_label_audit.sqlite"
OLD = PROJECT / "data" / "pilot_approved_keep"
OUT = PROJECT / "data" / "scale_probe_10k"
SEED = 20261018


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    return records


def write_atomic(path: Path, obj: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def build_selection(per_batch: int) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT batch, stt, entity_id, entity_name, url, reviewer, filename
        FROM canonical_role
        WHERE sheet_role='reviewed'
          AND COALESCE(TRIM(decision),'')=''
          AND COALESCE(TRIM(reviewer),'')<>''
          AND url LIKE 'http%'
        ORDER BY batch, url, entity_id
    """).fetchall()
    con.close()

    links = defaultdict(list)
    by_batch = defaultdict(dict)
    for row in rows:
        url = row["url"]
        link = {"batch": row["batch"], "entity_id": row["entity_id"],
                "entity_name": row["entity_name"], "stt": row["stt"]}
        links[url].append(link)
        by_batch[row["batch"]].setdefault(url, {
            "batch": row["batch"],
            "stt": row["stt"],
            "entity_id": row["entity_id"],
            "entity_name": row["entity_name"],
            "source_url": url,
            "source_workbook": row["filename"],
            "human_label": "keep",
            "label_source": "canonical reviewed keep; latest recheck overrides final",
        })

    previous = read_jsonl(OLD / "manifest.jsonl")
    previous_by_batch = defaultdict(list)
    for row in previous:
        previous_by_batch[row["batch"]].append(row["source_url"])

    selected = []
    used = set()
    reserved_old_urls = {url for urls in previous_by_batch.values() for url in urls}
    for batch in range(1, 9):
        carry = previous_by_batch[batch]
        if len(carry) > per_batch:
            raise ValueError(f"Batch {batch}: old pilot exceeds requested sample")
        for url in carry:
            if url not in by_batch[batch]:
                raise ValueError(f"Old pilot URL missing from canonical reviewed-keep data: {url}")
            if url in used:
                raise ValueError(f"Duplicate URL across batches: {url}")
            record = dict(by_batch[batch][url])
            record["entity_links"] = links[url]
            record["reused_pilot"] = True
            selected.append(record)
            used.add(url)

        candidates = [row for url, row in by_batch[batch].items() if url not in used and url not in reserved_old_urls]
        random.Random(SEED + batch).shuffle(candidates)
        needed = per_batch - len(carry)
        if len(candidates) < needed:
            raise ValueError(f"Batch {batch}: need {needed}, only {len(candidates)} URLs")
        for row in candidates[:needed]:
            record = dict(row)
            record["entity_links"] = links[row["source_url"]]
            record["reused_pilot"] = False
            selected.append(record)
            used.add(row["source_url"])
    return selected


def load_or_create_selection(per_batch: int) -> list[dict]:
    path = OUT / "selection.jsonl"
    if path.exists():
        selected = read_jsonl(path)
        if len(selected) != 8 * per_batch:
            raise ValueError(f"Existing selection has {len(selected)} rows, expected {8 * per_batch}")
        return selected
    selected = build_selection(per_batch)
    temp = path.with_suffix(".jsonl.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        for row in selected:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temp, path)
    return selected


def retryable(result: dict) -> bool:
    status = result.get("http_status")
    error = result.get("error") or ""
    return status in (408, 429, 500, 502, 503, 504) or any(
        word in error for word in ("URLError", "TimeoutError", "ConnectionResetError", "RemoteDisconnected")
    )


def process_one(record: dict, image_dir: Path) -> dict:
    result = download_one(record, image_dir, 768)
    attempts = 1
    if result["status"] != "valid_image" and retryable(result):
        time.sleep(1.5)
        result = download_one(record, image_dir, 768)
        attempts = 2
    result["attempts"] = attempts
    return result


def keep_system_awake() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)


def restore_sleep_policy() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-batch", type=int, default=1250)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()
    if args.per_batch < 125:
        raise ValueError("per-batch must include the existing 125 approved URLs")

    OUT.mkdir(parents=True, exist_ok=True)
    image_dir = OUT / "images"
    image_dir.mkdir(exist_ok=True)
    selection = load_or_create_selection(args.per_batch)
    selected_by_url = {row["source_url"]: row for row in selection}
    if len(selected_by_url) != len(selection):
        raise ValueError("Selection contains repeated URLs")

    result_path = OUT / "results.jsonl"
    results = {row["source_url"]: row for row in read_jsonl(result_path)}
    previous = {row["source_url"]: row for row in read_jsonl(OLD / "manifest.jsonl")}
    reused = 0
    with result_path.open("a", encoding="utf-8", newline="\n") as log:
        for url, record in selected_by_url.items():
            if url in results or url not in previous:
                continue
            old = previous[url]
            if old["status"] != "valid_image":
                continue
            source_path = OLD / "images" / old["image_path"]
            if not source_path.exists():
                continue
            target_path = image_dir / old["image_path"]
            if not target_path.exists():
                shutil.copy2(source_path, target_path)
            result = {**record, **old, "entity_links": record["entity_links"],
                      "reused_pilot": True, "attempts": 0}
            results[url] = result
            log.write(json.dumps(result, ensure_ascii=False) + "\n")
            reused += 1
        log.flush()

    pending = [row for row in selection if row["source_url"] not in results
               or (args.retry_errors and results[row["source_url"]]["status"] != "valid_image")]
    print(f"selected={len(selection)} reused={reused} completed={len(results)} pending={len(pending)}", flush=True)
    started = time.perf_counter()
    keep_system_awake()
    try:
        with result_path.open("a", encoding="utf-8", newline="\n") as log:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(process_one, row, image_dir): row["source_url"] for row in pending}
                for i, future in enumerate(as_completed(futures), 1):
                    row = future.result()
                    url = row["source_url"]
                    results[url] = row
                    log.write(json.dumps(row, ensure_ascii=False) + "\n")
                    log.flush()
                    if i % 250 == 0 or i == len(pending):
                        valid = sum(x["status"] == "valid_image" for x in results.values())
                        print(f"processed_new={i}/{len(pending)} total={len(results)}/{len(selection)} valid={valid}", flush=True)
                        write_atomic(OUT / "progress.json", {
                            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                            "selected": len(selection), "completed": len(results),
                            "valid": valid, "pending": len(selection) - len(results),
                        })
    finally:
        restore_sleep_policy()

    valid_rows = [row for row in results.values() if row["status"] == "valid_image"]
    unique_sha = {row["source_sha256"] for row in valid_rows}
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Team 3 canonical reviewed-keep rows; latest recheck wins; reviewer required",
        "seed": SEED, "per_batch": args.per_batch, "workers": args.workers,
        "selected_urls": len(selection), "completed_urls": len(results),
        "valid_images": len(valid_rows),
        "failed_urls": len(results) - len(valid_rows),
        "unique_source_sha256": len(unique_sha),
        "exact_byte_duplicates": len(valid_rows) - len(unique_sha),
        "valid_rate": round(len(valid_rows) / len(results), 5),
        "unique_byte_yield": round(len(unique_sha) / len(results), 5),
        "run_seconds_excluding_reused": round(time.perf_counter() - started, 2),
        "source_bytes_valid": sum(row.get("source_bytes", 0) for row in valid_rows),
        "processed_bytes": sum(row.get("processed_bytes", 0) for row in valid_rows),
        "errors_by_http_status": dict(Counter(str(row.get("http_status")) for row in results.values()
                                              if row["status"] != "valid_image")),
        "limitations": [
            "Sampled approved URLs only; not an evaluation of bad-image detection.",
            "Exact-byte uniqueness only; perceptual duplicates and entity relevance remain unmeasured.",
            "Retries and source rate limits can change yield and throughput.",
        ],
    }
    write_atomic(OUT / "report.json", report)
    print("REPORT", json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
