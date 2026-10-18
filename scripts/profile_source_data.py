"""Read-only metadata profile of the eight assignment workbooks used by the pilot.

This profiles source rows. Available DONE workbooks label only the user's assigned subset.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

from openpyxl import load_workbook

from pilot_images import SOURCES


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def profile(workspace: Path) -> dict:
    per_batch = []
    entity_urls: dict[str, set[str]] = defaultdict(set)
    url_entities: dict[str, set[str]] = defaultdict(set)
    descriptions = []
    domains = Counter()
    missing = Counter()
    all_urls = set()
    total_rows = 0
    per_batch_unique_total = 0

    for batch, rel_path in SOURCES:
        path = workspace / rel_path
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb["Ảnh đã thẩm định"]
        ws.reset_dimensions()
        rows = ws.iter_rows(values_only=True)
        headers = next(rows)
        columns = {str(value).strip(): i for i, value in enumerate(headers) if value is not None}
        url_col = columns["URL Ảnh"]
        entity_col = columns["Entity ID"]
        description_col = columns["Mô tả ảnh (Gemini Vision)"]
        resolution_col = columns["Độ phân giải"]
        source_page_col = columns["Trang nguồn"]
        title_col = columns["Tiêu đề trang nguồn"]
        urls_in_batch = set()
        rows_in_batch = 0

        for row in rows:
            if not any(value is not None for value in row):
                continue
            rows_in_batch += 1
            total_rows += 1
            entity = row[entity_col]
            description = row[description_col]
            url = row[url_col]
            if entity is None or str(entity).strip() == "":
                missing["entity_id"] += 1
            if description is None or str(description).strip() == "":
                missing["description"] += 1
            else:
                descriptions.append(len(str(description).strip()))
            if row[resolution_col] is None or str(row[resolution_col]).strip() == "":
                missing["resolution_metadata"] += 1
            if row[source_page_col] is None or str(row[source_page_col]).strip() == "":
                missing["source_page"] += 1
            if row[title_col] is None or str(row[title_col]).strip() == "":
                missing["source_title"] += 1

            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                missing["http_url"] += 1
                continue
            urls_in_batch.add(url)
            all_urls.add(url)
            hostname = (urlsplit(url).hostname or "").lower()
            if hostname.startswith("www."):
                hostname = hostname[4:]
            domains[hostname] += 1
            if entity is not None and str(entity).strip():
                entity_id = str(entity).strip()
                entity_urls[entity_id].add(url)
                url_entities[url].add(entity_id)

        wb.close()
        per_batch_unique_total += len(urls_in_batch)
        per_batch.append(
            {
                "batch": batch,
                "source_file": str(path),
                "rows": rows_in_batch,
                "distinct_urls_in_batch": len(urls_in_batch),
            }
        )
        print(f"profiled batch {batch}: {rows_in_batch} rows", flush=True)

    counts = [len(urls) for urls in entity_urls.values()]
    entities_ge5 = {entity for entity, urls in entity_urls.items() if len(urls) >= 5}
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Ảnh đã thẩm định sheets in the eight PHAN_CONG workbooks used by the pilot; source metadata only",
        "rows": total_rows,
        "distinct_urls": len(all_urls),
        "distinct_entity_ids": len(entity_urls),
        "distinct_entity_url_pairs": sum(counts),
        "repeat_url_rows_within_batches": total_rows - per_batch_unique_total,
        "repeat_urls_across_batches": per_batch_unique_total - len(all_urls),
        "urls_assigned_to_multiple_entities": sum(len(ids) > 1 for ids in url_entities.values()),
        "missing_counts": dict(missing),
        "entity_distinct_url_distribution": {
            "min": min(counts),
            "median": median(counts),
            "p90": percentile(counts, 0.90),
            "p95": percentile(counts, 0.95),
            "max": max(counts),
            "entities_with_fewer_than_5_urls": sum(count < 5 for count in counts),
            "entities_with_at_least_5_urls": len(entities_ge5),
        },
        "description_character_distribution": {
            "median": median(descriptions),
            "p90": percentile(descriptions, 0.90),
            "max": max(descriptions),
        },
        "top_source_domains_by_row_count": domains.most_common(10),
        "per_batch": per_batch,
        "limitations": [
            "PHAN_CONG workbooks are assignment sources. Their rows are not equivalent to final human-approved images.",
            "Available DONE workbooks label only Nguyễn Tuấn Thành's assigned subset, not the full source pool.",
            "URL counts do not establish image downloadability, content uniqueness, or correctness.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path(r"C:\Drive_D\Crawl Data"))
    parser.add_argument("--output", type=Path, default=Path("data/analysis/source_profile.json"))
    args = parser.parse_args()
    result = profile(args.workspace)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
