"""Run a local NSFW model in shadow mode on the decoded pilot.

Outputs scores and stage timings only. It never changes keep/reject decisions.
The canonical mixed keep/x label is NOT an NSFW ground truth label.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
MODEL_ID = "hf-hub:Marqo/nsfw-image-detection-384"
MODEL_REVISION = "0c26ec22111b83f106d72a55f611ec35962bcb65"
WEIGHT_SHA256 = "6bf2e0f64a1d20169736c2836e3a787b12379fdc08ba87f7d94a7a3d58eeefce"
CONFIG_SHA256 = "ae848d1dca0aeccd38f2ebb4b1bb47219cdc7f34e3ddc50c8d6430abe74d79b4"
THRESHOLDS = (0.5, 0.9, 0.99)


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def select_distinct_sample(manifest: Path, sample: int, seed: int) -> tuple[list[dict], int]:
    """Uniform reservoir sample of decoded asset paths, deduplicated in memory."""
    if sample < 1:
        raise ValueError("sample must be positive")
    rng = random.Random(seed)
    selected: list[dict] = []
    seen: set[str] = set()
    for row in read_jsonl(manifest):
        path = row.get("image_path")
        if row.get("status") != "valid_image" or not path or path in seen:
            continue
        seen.add(path)
        ordinal = len(seen)
        if len(selected) < sample:
            selected.append(row)
        else:
            slot = rng.randrange(ordinal)
            if slot < sample:
                selected[slot] = row
    if len(selected) < sample:
        raise ValueError(f"Only {len(selected)} distinct valid assets; requested {sample}")
    selected.sort(key=lambda row: str(row["image_path"]))
    return selected, len(seen)


def percentile_ms(values: list[float], percentile: int) -> float | None:
    return round(float(np.percentile(values, percentile) * 1000), 2) if values else None


def run(pilot: Path, labels_csv: Path | None, model_dir: Path, output: Path, report_path: Path,
        batch_size: int, limit: int | None = None, manifest: Path | None = None,
        image_root: Path | None = None, sample: int | None = None,
        seed: int = 20261021) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if sample is not None and limit is not None:
        raise ValueError("Choose --sample or --limit, not both")
    labels = {}
    if labels_csv is not None:
        with labels_csv.open(encoding="utf-8-sig", newline="") as source:
            labels = {(row["batch"], row["entity_id"], row["source_url"]): row["human_label"]
                      for row in csv.DictReader(source)}
    source_manifest = manifest or pilot / "indexed_manifest.jsonl"
    root = image_root or pilot / "images"
    if sample is not None:
        rows, available = select_distinct_sample(source_manifest, sample, seed)
    else:
        rows = list(read_jsonl(source_manifest))
        available = None
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        raise ValueError("No decoded pilot images")

    import timm
    import torch
    from safetensors.torch import load_file

    device = "cuda" if torch.cuda.is_available() else "cpu"
    started_load = time.perf_counter()
    config_path = model_dir / "config.json"
    weights_path = model_dir / "model.safetensors"
    if not config_path.is_file() or not weights_path.is_file():
        raise FileNotFoundError(f"Download pinned model files into {model_dir} first")
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != CONFIG_SHA256:
        raise ValueError("Model config checksum does not match pinned revision")
    digest = hashlib.sha256()
    with weights_path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != WEIGHT_SHA256:
        raise ValueError("Model weight checksum does not match pinned revision")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    model = timm.create_model(
        config["architecture"], pretrained=False,
        num_classes=config["num_classes"], pretrained_cfg=config["pretrained_cfg"]
    )
    model.load_state_dict(load_file(weights_path), strict=True)
    model = model.eval().to(device)
    transform = timm.data.create_transform(
        **timm.data.resolve_model_data_config(model), is_training=False
    )
    names = [str(name).lower() for name in config["label_names"]]
    if "nsfw" not in names:
        raise ValueError(f"Model classes do not include nsfw: {names}")
    unsafe_index = names.index("nsfw")
    load_seconds = time.perf_counter() - started_load

    # Warm model kernels without measuring model startup as per-image inference.
    with Image.open(root / str(rows[0]["image_path"]).replace("\\", "/")) as source:
        warm = transform(source.convert("RGB")).unsqueeze(0).to(device)
    with torch.inference_mode():
        model(warm)
    if device == "cuda":
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()

    output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    durations: list[float] = []
    label_counts: Counter[str] = Counter()
    flagged = {str(threshold): Counter() for threshold in THRESHOLDS}
    scores_by_label: dict[str, list[float]] = {}
    scored = 0
    started_inference = time.perf_counter()
    with temporary.open("w", encoding="utf-8", newline="\n") as sink, torch.inference_mode():
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            tensors = []
            for row in batch:
                with Image.open(root / str(row["image_path"]).replace("\\", "/")) as source:
                    tensors.append(transform(source.convert("RGB")))
            images = torch.stack(tensors).to(device)
            if device == "cuda":
                torch.cuda.synchronize()
            batch_started = time.perf_counter()
            probabilities = model(images).softmax(dim=-1)[:, unsafe_index].cpu().numpy()
            if device == "cuda":
                torch.cuda.synchronize()
            durations.append(time.perf_counter() - batch_started)
            for row, probability in zip(batch, probabilities, strict=True):
                key = (str(row["batch"]), str(row["entity_id"]), row["source_url"])
                human_label = labels.get(key, "unknown")
                label_counts[human_label] += 1
                score = float(probability)
                scores_by_label.setdefault(human_label, []).append(score)
                for threshold in THRESHOLDS:
                    if score >= threshold:
                        flagged[str(threshold)][human_label] += 1
                sink.write(json.dumps({
                    "batch": row["batch"], "entity_id": row["entity_id"],
                    "image_path": row["image_path"], "human_label": human_label,
                    "nsfw_probability": round(score, 6),
                }, ensure_ascii=False) + "\n")
                scored += 1
    inference_seconds = time.perf_counter() - started_inference
    os.replace(temporary, output)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_ID, "model_revision": MODEL_REVISION,
        "weight_sha256": WEIGHT_SHA256, "config_sha256": CONFIG_SHA256,
        "label_names": names,
        "device": device, "batch_size": batch_size, "images": scored,
        "sampling": {"method": "uniform_without_replacement_distinct_valid_asset_paths",
                     "seed": seed, "available_distinct_paths": available,
                     "sampled_assets": sample} if sample is not None else None,
        "model_load_seconds": round(load_seconds, 3),
        "inference_and_preprocessing_seconds": round(inference_seconds, 3),
        "images_per_second": round(scored / inference_seconds, 2),
        "batch_model_p50_ms": percentile_ms(durations, 50),
        "batch_model_p95_ms": percentile_ms(durations, 95),
        "peak_gpu_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20, 1)
        if device == "cuda" else None,
        "human_mixed_label_counts": dict(label_counts),
        "score_quantiles_by_mixed_human_label": {
            label: {"p50": round(float(np.percentile(scores, 50)), 4),
                    "p95": round(float(np.percentile(scores, 95)), 4),
                    "p99": round(float(np.percentile(scores, 99)), 4),
                    "max": round(max(scores), 4)}
            for label, scores in scores_by_label.items()
        },
        "flag_counts_by_mixed_human_label": {key: dict(value) for key, value in flagged.items()},
        "score_file": str(output),
        "policy": "shadow_only_no_automatic_rejection",
        "limitations": [
            "The evaluated images have no independently confirmed NSFW-positive labels, so recall and precision cannot be measured.",
            "Canonical keep/x labels, when supplied, mix many quality reasons and are not an NSFW benchmark.",
            "Threshold counts are workload observations, not accuracy or safety claims.",
            "The model card's reported metrics are on its own dataset, not on this Team 3 corpus.",
            "Reported throughput excludes HTTP download, dHash, source checks, and human review.",
            "The sampler holds a set of seen paths in RAM and is not a 100M-scale sampling implementation.",
        ],
    }
    temp_report = report_path.with_suffix(report_path.suffix + ".tmp")
    temp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_report, report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=PROJECT / "data/pilot")
    parser.add_argument("--labels-csv", type=Path,
                        default=PROJECT / "data/analysis/processing_pilot_label_audit.csv")
    parser.add_argument("--no-labels", action="store_true",
                        help="Do not join the pilot's mixed keep/reject labels")
    parser.add_argument("--manifest", type=Path,
                        help="Override input manifest, e.g. the full assembled corpus")
    parser.add_argument("--image-root", type=Path,
                        help="Root for image_path in --manifest")
    parser.add_argument("--sample", type=int,
                        help="Seeded uniform sample of distinct valid asset paths")
    parser.add_argument("--seed", type=int, default=20261021)
    parser.add_argument("--model-dir", type=Path, default=PROJECT / ".cache/nsfw_model")
    parser.add_argument("--output", type=Path, default=PROJECT / "data/analysis/safety_shadow_scores.jsonl")
    parser.add_argument("--report", type=Path, default=PROJECT / "data/analysis/safety_shadow_report.json")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    print(json.dumps(run(args.pilot, None if args.no_labels else args.labels_csv,
                         args.model_dir, args.output, args.report, args.batch_size,
                         args.limit, args.manifest, args.image_root, args.sample,
                         args.seed), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
