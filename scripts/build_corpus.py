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
PROBE = PROJECT / "data" / "scale_probe_10k"
OUT = PROJECT / "data" / "corpus_v1"
SEED = 20261018
MIN_FREE_BYTES = 8 * 1024**3


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_atomic(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def selection() -> list[dict]:
    path = OUT / "selection.jsonl"
    if path.exists():
        return read_jsonl(path)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT batch, stt, entity_id, entity_name, url, reviewer, filename
        FROM canonical_role
        WHERE sheet_role='reviewed'
          AND COALESCE(TRIM(decision),'')=''
          AND COALESCE(TRIM(reviewer),'')<>''
          AND url IS NOT NULL
        ORDER BY batch, url, entity_id
    """).fetchall()
    con.close()
    by_url = {}
    for row in rows:
        url = row["url"]
        if not url.startswith(("http://", "https://")):
            continue
        if url not in by_url:
            by_url[url] = {
                "batch": row["batch"],
                "stt": row["stt"],
                "entity_id": row["entity_id"],
                "entity_name": row["entity_name"],
                "source_url": url,
                "source_workbook": row["filename"],
                "human_label": "keep",
                "label_source": "canonical reviewed keep; latest recheck overrides final",
                "entity_links": [],
            }
        by_url[url]["entity_links"].append({
            "batch": row["batch"],
            "entity_id": row["entity_id"],
            "entity_name": row["entity_name"],
            "stt": row["stt"],
        })
    records = list(by_url.values())
    random.Random(SEED).shuffle(records)
    temp = path.with_suffix(".jsonl.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        for row in records:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temp, path)
    return records


def retryable(result: dict) -> bool:
    status = result.get("http_status")
    error = result.get("error") or ""
    return status in (408, 429, 500, 502, 503, 504) or any(
        word in error for word in (
            "getaddrinfo failed", "timed out", "TimeoutError", "ConnectionResetError",
            "RemoteDisconnected", "Temporary failure in name resolution",
        )
    )


def dns_failure(result: dict) -> bool:
    error = result.get("error") or ""
    return "getaddrinfo failed" in error or "Temporary failure in name resolution" in error


def process_one(record: dict, incoming_dir: Path) -> dict:
    result = download_one(record, incoming_dir, 768)
    attempts = 1
    if result["status"] != "valid_image" and retryable(result):
        time.sleep(1.5)
        result = download_one(record, incoming_dir, 768)
        attempts = 2
    result["attempts"] = attempts
    return result


def store_image(result: dict, incoming_dir: Path, source_path: Path | None = None) -> dict:
    sha = result["source_sha256"]
    dest = OUT / "images" / sha[:2] / (sha + ".webp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source_path is not None:
        if not dest.exists():
            try:
                os.link(source_path, dest)
            except OSError:
                shutil.copy2(source_path, dest)
    else:
        temp = incoming_dir / result["image_path"]
        if dest.exists():
            temp.unlink(missing_ok=True)
        else:
            os.replace(temp, dest)
    result["image_path"] = str(dest.relative_to(OUT))
    result["stored_bytes"] = dest.stat().st_size
    return result


def keep_awake() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)


def allow_sleep() -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    incoming_dir = OUT / "incoming"
    incoming_dir.mkdir(exist_ok=True)
    selected = selection()
    by_url = {row["source_url"]: row for row in selected}
    if len(by_url) != len(selected):
        raise ValueError("Selection contains duplicate URLs")
    print(f"selected={len(selected)}", flush=True)

    log_path = OUT / "results.jsonl"
    results = {row["source_url"]: row for row in read_jsonl(log_path)}
    probe_results = {row["source_url"]: row for row in read_jsonl(PROBE / "results.jsonl")}
    reused = 0
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        for url, original in probe_results.items():
            if url not in by_url or url in results or original["status"] != "valid_image":
                continue
            source_path = PROBE / "images" / original["image_path"]
            if not source_path.exists():
                continue
            result = {**by_url[url], **original, "entity_links": by_url[url]["entity_links"],
                      "reused_probe": True, "attempts": 0}
            result = store_image(result, incoming_dir, source_path)
            results[url] = result
            log.write(json.dumps(result, ensure_ascii=False) + "\n")
            reused += 1
        log.flush()

    pending = [row for row in selected if row["source_url"] not in results
               or (args.retry_errors and retryable(results[row["source_url"]]))]
    print(f"reused={reused} completed={len(results)} pending={len(pending)}", flush=True)
    if shutil.disk_usage(OUT).free < MIN_FREE_BYTES:
        raise RuntimeError("Less than 8 GiB free; corpus run not started")
    started = time.perf_counter()
    keep_awake()
    try:
        with log_path.open("a", encoding="utf-8", newline="\n") as log:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                processed = 0
                for offset in range(0, len(pending), 500):
                    chunk = pending[offset:offset + 500]
                    futures = [pool.submit(process_one, row, incoming_dir) for row in chunk]
                    chunk_dns_failures = 0
                    for future in as_completed(futures):
                        result = future.result()
                        if dns_failure(result):
                            chunk_dns_failures += 1
                        if result["status"] == "valid_image":
                            result = store_image(result, incoming_dir)
                        results[result["source_url"]] = result
                        log.write(json.dumps(result, ensure_ascii=False) + "\n")
                        log.flush()
                        processed += 1
                    valid = sum(row["status"] == "valid_image" for row in results.values())
                    unique_sha = {row["source_sha256"] for row in results.values() if row["status"] == "valid_image"}
                    progress = {
                        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                        "selected_urls": len(selected),
                        "completed_urls": len(results),
                        "valid_images": valid,
                        "unique_byte_images": len(unique_sha),
                        "pending_urls": len(selected) - len(results),
                        "free_bytes": shutil.disk_usage(OUT).free,
                    }
                    write_atomic(OUT / "progress.json", progress)
                    print(f"new={processed}/{len(pending)} complete={len(results)}/{len(selected)} valid={valid} unique={len(unique_sha)}", flush=True)
                    if chunk_dns_failures >= max(20, len(chunk) // 10):
                        raise RuntimeError(
                            f"Stopped safely: DNS failed for {chunk_dns_failures}/{len(chunk)} URLs in the latest chunk; resume with --retry-errors"
                        )
                    if progress["free_bytes"] < MIN_FREE_BYTES:
                        raise RuntimeError("Stopped safely: less than 8 GiB free")
    finally:
        allow_sleep()

    valid_rows = [row for row in results.values() if row["status"] == "valid_image"]
    unique_files = {row["source_sha256"]: row["image_path"] for row in valid_rows}
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Team 3 canonical reviewed-keep, latest recheck wins, reviewer required",
        "selected_urls": len(selected),
        "completed_urls": len(results),
        "valid_images": len(valid_rows),
        "unique_byte_images": len(unique_files),
        "failed_urls": len(results) - len(valid_rows),
        "unique_byte_yield": round(len(unique_files) / len(results), 5),
        "run_seconds_excluding_reused": round(time.perf_counter() - started, 2),
        "physical_processed_bytes": sum((OUT / path).stat().st_size for path in unique_files.values()),
        "errors_by_http_status": dict(Counter(str(row.get("http_status")) for row in results.values()
                                              if row["status"] != "valid_image")),
        "limitations": [
            "Reviewed-keep decisions are human labels, not proof of current entity relevance or licensing.",
            "Exact-byte uniqueness only; perceptual duplicates need a separate audit.",
        ],
    }
    write_atomic(OUT / "report.json", report)
    print("REPORT", json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
