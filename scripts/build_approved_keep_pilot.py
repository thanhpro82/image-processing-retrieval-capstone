from __future__ import annotations
import hashlib, json, random, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from pilot_images import download_one

DB = PROJECT / "data" / "analysis" / "team3_label_audit.sqlite"
OUT = PROJECT / "data" / "pilot_approved_keep"
IMAGES = OUT / "images"
PER_BATCH = 125
SEED = 20261018
WORKERS = 8
IMAGES.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
all_rows = con.execute("""
    SELECT batch, stt, entity_name, entity_id, url, reviewer, note, filename
    FROM canonical_role
    WHERE sheet_role='reviewed'
      AND COALESCE(TRIM(decision),'')=''
      AND COALESCE(TRIM(reviewer),'')<>''
      AND url IS NOT NULL AND TRIM(url)<>''
    ORDER BY batch, url, entity_id
""").fetchall()
con.close()

rng = random.Random(SEED)
selected = []
used_urls = set()
by_batch = {}
for batch in range(1, 9):
    candidates = []
    local_seen = set()
    for row in all_rows:
        if row["batch"] != batch:
            continue
        url = row["url"]
        if url in used_urls or url in local_seen:
            continue
        local_seen.add(url)
        candidates.append({
            "batch": batch,
            "stt": row["stt"],
            "entity_name": row["entity_name"],
            "entity_id": row["entity_id"],
            "source_url": url,
            "reviewer": row["reviewer"],
            "review_note": row["note"],
            "source_workbook": row["filename"],
            "human_label": "keep",
            "label_source": "latest canonical reviewed-sheet decision; blank means keep",
        })
    if len(candidates) < PER_BATCH:
        raise RuntimeError(f"Batch {batch}: only {len(candidates)} eligible URLs")
    chosen = rng.sample(candidates, PER_BATCH)
    selected.extend(chosen)
    used_urls.update(x["source_url"] for x in chosen)
    by_batch[str(batch)] = {"eligible_unique_urls": len(candidates), "sampled": len(chosen)}
    print(f"sampled batch {batch}: {len(chosen)} verified-keep URLs", flush=True)

start = time.perf_counter()
results = []
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futures = [pool.submit(download_one, row, IMAGES, 768) for row in selected]
    for i, future in enumerate(as_completed(futures), 1):
        results.append(future.result())
        if i % 100 == 0 or i == len(futures):
            valid_n = sum(x["status"] == "valid_image" for x in results)
            print(f"downloaded {i}/{len(futures)}; valid={valid_n}", flush=True)
elapsed = time.perf_counter() - start

seen_sha = {}
unique_images = 0
for row in results:
    if row["status"] != "valid_image":
        continue
    sha = row["source_sha256"]
    if sha in seen_sha:
        (IMAGES / row["image_path"]).unlink(missing_ok=True)
        row["duplicate_of"] = seen_sha[sha]
        row["image_path"] = seen_sha[sha]
        row["processed_bytes"] = 0
    else:
        seen_sha[sha] = row["image_path"]
        unique_images += 1

results.sort(key=lambda x: (x["batch"], str(x.get("stt")), x["source_url"]))
with (OUT / "manifest.jsonl").open("w", encoding="utf-8", newline="\n") as f:
    for row in results:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

valid = [x for x in results if x["status"] == "valid_image"]
report = {
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "sample_method": "125 unique URLs per batch sampled from canonical reviewed role with latest keep label (blank), non-empty reviewer; latest recheck overrides final",
    "seed": SEED,
    "sampled_urls": len(results),
    "valid_images": len(valid),
    "download_or_decode_errors": len(results) - len(valid),
    "unique_byte_images": unique_images,
    "valid_rate": round(len(valid) / len(results), 4),
    "elapsed_download_and_process_sec": round(elapsed, 2),
    "valid_images_per_sec": round(len(valid) / elapsed, 3) if elapsed else None,
    "source_bytes_valid": sum(x["source_bytes"] for x in valid),
    "processed_bytes_unique": sum(x.get("processed_bytes", 0) for x in valid),
    "by_batch": by_batch,
    "limitations": [
        "This is a clean/approved-image operational pilot; it cannot measure how well a filter detects rejected images.",
        "URL liveness and successful decoding do not guarantee correct entity relevance.",
        "Exact-byte duplicate removal only."
    ]
}
(OUT / "pilot_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print("REPORT", json.dumps(report, ensure_ascii=False), flush=True)
print("MANIFEST", OUT / "manifest.jsonl", flush=True)
