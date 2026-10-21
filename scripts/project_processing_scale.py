"""Project measured processing-stage resources; never imply a completed 10–100M run."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def scenario(count: int, cpu: dict, pilot: dict, machine_hour_usd: float | None,
             storage_gb_month_usd: float | None, safety: dict | None = None,
             gpu_hour_usd: float | None = None) -> dict:
    cpu_hours = count / cpu["throughput_assets_per_second"] / 3600
    safety_hours = count / safety["images_per_second"] / 3600 if safety else None
    source_bytes = count * pilot["mean_source_bytes_per_valid"]
    stored_bytes = count * cpu["mean_bytes_per_asset"]
    return {
        "valid_assets": count,
        "cpu_stage_machine_hours_at_sample_throughput": round(cpu_hours, 2),
        "source_download_bytes_using_pilot_mean": round(source_bytes),
        "processed_storage_bytes_using_cpu_sample_mean": round(stored_bytes),
        "download_hours_at_pilot_rate_without_parallel_scale": round(
            count / pilot["valid_images_per_sec"] / 3600, 2),
        "adult_content_shadow_gpu_hours_at_pilot_rate": round(safety_hours, 2)
        if safety_hours is not None else None,
        "cpu_stage_usd_if_machine_rate_supplied": (
            round(cpu_hours * machine_hour_usd, 2) if machine_hour_usd is not None else None),
        "one_month_processed_storage_usd_if_rate_supplied": (
            round(stored_bytes / 1_000_000_000 * storage_gb_month_usd, 2)
            if storage_gb_month_usd is not None else None),
        "adult_content_shadow_usd_if_gpu_rate_supplied": (
            round(safety_hours * gpu_hour_usd, 2)
            if safety_hours is not None and gpu_hour_usd is not None else None),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-report", type=Path,
                        default=PROJECT / "data/corpus_v1/processing_cpu_benchmark_report.json")
    parser.add_argument("--pilot-report", type=Path,
                        default=PROJECT / "data/pilot/pilot_report.json")
    parser.add_argument("--output", type=Path,
                        default=PROJECT / "data/analysis/processing_scale_projection.json")
    parser.add_argument("--safety-report", type=Path,
                        help="Optional measured adult-content shadow report; not a validated safety gate")
    parser.add_argument("--machine-hour-usd", type=float)
    parser.add_argument("--gpu-hour-usd", type=float)
    parser.add_argument("--storage-gb-month-usd", type=float)
    args = parser.parse_args()
    for name in ("machine_hour_usd", "gpu_hour_usd", "storage_gb_month_usd"):
        value = getattr(args, name)
        if value is not None and value < 0:
            parser.error(f"--{name.replace('_', '-')} must be nonnegative")
    cpu = json.loads(args.cpu_report.read_text(encoding="utf-8"))
    pilot = json.loads(args.pilot_report.read_text(encoding="utf-8"))
    safety = json.loads(args.safety_report.read_text(encoding="utf-8")) if args.safety_report else None
    if safety and (safety.get("images_per_second", 0) <= 0 or safety.get("policy") != "shadow_only_no_automatic_rejection"):
        parser.error("--safety-report must contain a measured positive shadow-only throughput")
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "Linear projection from measured local stages, not a 10–100M benchmark",
        "cpu_sample_assets": cpu["sampled_assets"],
        "cpu_sample_throughput_assets_per_second": cpu["throughput_assets_per_second"],
        "download_pilot_valid_images": pilot["valid_images"],
        "download_pilot_valid_images_per_second": pilot["valid_images_per_sec"],
        "adult_content_shadow_pilot_images": safety["images"] if safety else None,
        "adult_content_shadow_pilot_images_per_second": safety["images_per_second"] if safety else None,
        "machine_hour_usd_input": args.machine_hour_usd,
        "gpu_hour_usd_input": args.gpu_hour_usd,
        "storage_gb_month_usd_input": args.storage_gb_month_usd,
        "scenarios": [scenario(n, cpu, pilot, args.machine_hour_usd,
                               args.storage_gb_month_usd, safety, args.gpu_hour_usd)
                      for n in (1_000_000, 10_000_000, 100_000_000)],
        "excluded_costs": ["other model inference and validated safety/semantic screening", "human review",
                           "network transfer and retries", "duplicate-index storage", "orchestration"],
        "limitations": [
            "CPU probe used already-downloaded human-approved WebP files; it is not end-to-end throughput.",
            "Download pilot sampled only 1,000 URLs. Its source bytes and rate are not a production SLA.",
            "CPU sample and download pilot have different processed image size distributions; storage uses the full-corpus CPU sample mean.",
            "Linear scaling ignores contention, remote host rate limits, failures and other model costs.",
            "The adult-content shadow run has no independently labeled NSFW positives; its throughput is not accuracy or a deployable gate.",
            "GPU hours, if supplied, count only the adult-content shadow stage and exclude preprocessing outside that script.",
            "USD fields are null until actual user-supplied unit rates are available; no total project cost is claimed.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
