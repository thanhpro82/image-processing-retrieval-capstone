"""Reproducible API latency/load probe on the real indexed corpus.

Generated labels are metadata-based relevance proxies, not human judgments.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parents[1]


def fold(value: str) -> str:
    value = value.lower().replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFKD", value)
                   if not unicodedata.combining(c))


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    left = int(position)
    return ordered[left] + (ordered[min(left + 1, len(ordered) - 1)] - ordered[left]) * (position - left)


def request(base: str, query: str) -> tuple[dict, float]:
    start = time.perf_counter()
    with urlopen(base + "/api/search?q=" + quote(query) + "&limit=10", timeout=10) as response:
        result = json.load(response)
    return result, (time.perf_counter() - start) * 1000


def timed_request(base: str, query: str) -> tuple[dict | None, float, str | None]:
    """Keep timeouts in the report instead of silently dropping a slow request."""
    started = time.perf_counter()
    try:
        answer, elapsed_ms = request(base, query)
        return answer, elapsed_ms, None
    except (OSError, TimeoutError) as exc:
        return None, (time.perf_counter() - started) * 1000, type(exc).__name__


def build_cases(db: Path, count: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as con:
        names = sorted({name.strip() for (text,) in con.execute("SELECT entity_names FROM images")
                        for name in text.split(" | ")
                        if 5 <= len(name.strip()) <= 65 and " và " not in name.lower()})
        region_map = defaultdict(set)
        name_regions = defaultdict(set)
        for name, region in con.execute("SELECT entity_name, region FROM entity_region"):
            if 5 <= len(name) <= 65 and region:
                region_map[region].add(name)
                name_regions[fold(name)].add(region)
    if len(names) < count * 2:
        raise ValueError("Not enough distinct entity names for benchmark")
    singles = rng.sample(names, count)
    cases = [{"type": "single_intent", "query": name, "expected_names": [name]} for name in singles]
    pairs = rng.sample(names, count * 2)
    cases.extend({"type": "multi_intent", "query": f"{pairs[2*i]} và {pairs[2*i+1]}",
                  "expected_names": pairs[2*i:2*i+2]} for i in range(count))
    # Only generate grounded cases where both names map to exactly one region.
    # Ambiguous references should be tested separately as deliberate no-answer cases.
    eligible = [
        (region, sorted(name for name in group
                        if len(name_regions[fold(name)]) == 1
                        and region in name_regions[fold(name)]))
        for region, group in region_map.items()
    ]
    eligible = [(region, group) for region, group in eligible if len(group) >= 2]
    if len(eligible) < count:
        raise ValueError("Not enough regions with two grounded entities")
    for region, group in rng.sample(eligible, count):
        target, reference = rng.sample(group, 2)
        cases.append({"type": "multi_hop", "query": f"{target} ở cùng tỉnh với {reference}",
                      "expected_names": [target], "expected_region": region,
                      "reference_entity": reference})
    return cases


def proxy_hit(case: dict, answer: dict) -> bool:
    if case["type"] == "multi_intent":
        groups = answer.get("intents", [])
        return len(groups) >= 2 and all(
            any(fold(expected) in fold(hit.get("entity_names", "")) for hit in group["results"])
            for expected, group in zip(case["expected_names"], groups, strict=True)
        )
    if case["type"] == "multi_hop" and fold(answer.get("resolved_region", "")) != fold(case["expected_region"]):
        return False
    expected = case["expected_names"][0]
    return any(fold(expected) in fold(hit.get("entity_names", "")) for hit in answer.get("results", []))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--corpus", type=Path, default=PROJECT / "data" / "corpus_v1")
    parser.add_argument("--per-type", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    corpus = args.corpus.resolve()
    try:
        with urlopen(args.base_url + "/healthz", timeout=10) as response:
            health = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise SystemExit(
            f"Demo health check failed at {args.base_url}/healthz ({exc}). "
            "Check the Docker host port; pass --base-url if another service owns it."
        ) from exc
    if health["indexed_images"] < 80000:
        raise ValueError("Benchmark requires at least 80,000 indexed real images")
    cases = build_cases(corpus / "search.sqlite", args.per_type, args.seed)
    for case in cases[:5]:
        request(args.base_url, case["query"])
    results = []
    for case in cases:
        for repeat in range(args.repeats):
            answer, elapsed_ms, error = timed_request(args.base_url, case["query"])
            results.append({**case, "repeat": repeat, "latency_ms": round(elapsed_ms, 2),
                            "proxy_hit": proxy_hit(case, answer) if answer else False,
                            "returned": len(answer.get("results", [])) if answer else 0,
                            "partial": answer.get("partial", False) if answer else False,
                            "error": error})

    # One simple concurrent load probe using the same deterministic queries.
    load_queries = [cases[i % len(cases)]["query"] for i in range(100)]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(timed_request, args.base_url, query): i
                   for i, query in enumerate(load_queries)}
        load_observations = []
        for future in as_completed(futures):
            answer, elapsed_ms, error = future.result()
            load_observations.append({"case_index": futures[future] % len(cases),
                                      "latency_ms": round(elapsed_ms, 2),
                                      "partial": answer.get("partial", False) if answer else False,
                                      "error": error})
        load_latencies = [row["latency_ms"] for row in load_observations]
    load_seconds = time.perf_counter() - started
    load_errors = sum(row["error"] is not None for row in load_observations)
    by_type = {}
    for kind in ("single_intent", "multi_intent", "multi_hop"):
        subset = [row for row in results if row["type"] == kind]
        values = [row["latency_ms"] for row in subset]
        by_type[kind] = {
            "requests": len(subset),
            "p50_ms": round(percentile(values, 50), 2),
            "p95_ms": round(percentile(values, 95), 2),
            "max_ms": round(max(values), 2),
            "over_3000ms": sum(value >= 3000 for value in values),
            "errors": sum(row["error"] is not None for row in subset),
            "partial_responses": sum(row["partial"] for row in subset),
            "metadata_proxy_hit_rate": round(sum(row["proxy_hit"] for row in subset) / len(subset), 4),
        }
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "indexed_images": health["indexed_images"],
        "seed": args.seed,
        "case_method": "Deterministic sample of indexed entity names and entity-region links; generated queries are metadata-based proxies",
        "cases_per_type": args.per_type,
        "repeats_per_case": args.repeats,
        "warmup_queries": min(5, len(cases)),
        "latency_scope": "HTTP GET /api/search to JSON response on full local SQLite index; excludes browser image rendering",
        "by_type": by_type,
        "load_probe": {"requests": len(load_queries), "workers": args.workers,
                       "elapsed_seconds": round(load_seconds, 3),
                       "successful_requests": len(load_queries) - load_errors,
                       "throughput_requests_per_second": round((len(load_queries) - load_errors) / load_seconds, 2),
                       "p95_ms": round(percentile(load_latencies, 95), 2),
                       "over_3000ms": sum(value >= 3000 for value in load_latencies),
                       "partial_responses": sum(row["partial"] for row in load_observations),
                       "errors": load_errors},
        "limitations": [
            "Proxy hits reflect metadata overlap, not independent human relevance judgments.",
            "Only entity→region multi-hop is implemented; family and other relations are unsupported.",
            "Results depend on hardware, concurrent jobs, cache warmth and corpus version.",
        ],
    }
    output = corpus / "api_benchmark_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (corpus / "api_benchmark_cases.jsonl").write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
    (corpus / "api_benchmark_observations.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results)
        + "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in load_observations),
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
