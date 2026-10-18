"""Evaluate a cheap image/entity relevance filter against Team 3 reviewed labels.

This is a pilot, not a deployable verifier. It deliberately excludes candidate-sheet
"x" labels because those also mean that the entity already had enough images.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import open_clip
import torch

PROJECT = Path(__file__).resolve().parents[1]
MODEL_ID = "hf-hub:laion/CLIP-ViT-B-32-laion2B-s34B-b79K"


def metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
    tp = int(np.sum((actual == 1) & (predicted == 1)))
    fp = int(np.sum((actual == 0) & (predicted == 1)))
    fn = int(np.sum((actual == 1) & (predicted == 0)))
    tn = int(np.sum((actual == 0) & (predicted == 0)))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    reject_precision = tn / (tn + fn) if tn + fn else 0.0
    reject_recall = tn / (tn + fp) if tn + fp else 0.0
    return {
        "n": len(actual), "tp_keep": tp, "fp_keep": fp,
        "fn_keep": fn, "tn_reject": tn,
        "accuracy": round((tp + tn) / len(actual), 4),
        "keep_precision": round(precision, 4),
        "keep_recall": round(recall, 4),
        "keep_f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
        "reject_precision": round(reject_precision, 4),
        "reject_recall": round(reject_recall, 4),
        "balanced_accuracy": round((recall + reject_recall) / 2, 4),
    }


def load_labels(db_path: Path) -> dict[tuple[int, str, str], dict]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT batch, entity_id, url, decision, note FROM canonical_role "
            "WHERE sheet_role='reviewed' AND decision IN ('', 'x')"
        )
        return {(int(batch), str(eid), url): {"keep": int(decision == ""), "note": note or ""}
                for batch, eid, url, decision, note in rows}
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=Path, default=PROJECT / "data" / "pilot")
    parser.add_argument("--database", type=Path, default=PROJECT / "data" / "analysis" / "team3_label_audit.sqlite")
    parser.add_argument("--output", type=Path, default=PROJECT / "data" / "analysis" / "processing_pilot_report.json")
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()

    manifest = [json.loads(line) for line in (args.pilot / "indexed_manifest.jsonl").open(encoding="utf-8")]
    vectors = np.load(args.pilot / "image_vectors.npy")
    if len(manifest) != len(vectors):
        raise ValueError("Image vector and manifest lengths differ")
    labels = load_labels(args.database)
    indices = []
    rows = []
    for index, row in enumerate(manifest):
        # Batch is part of the canonical source key; entity IDs may repeat across batches.
        label = labels.get((int(row.get("batch")), str(row.get("entity_id")), row.get("source_url")))
        if label is None or not row.get("entity_name"):
            continue
        indices.append(index)
        rows.append({**row, **label})
    if len(rows) < 100:
        raise ValueError(f"Too few labeled, valid pilot images: {len(rows)}")

    names = sorted({row["entity_name"] for row in rows})
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _ = open_clip.create_model_from_pretrained(MODEL_ID)
    model = model.eval().to(device)
    tokenizer = open_clip.get_tokenizer(MODEL_ID)
    text_vectors = []
    started = time.perf_counter()
    with torch.inference_mode():
        for start in range(0, len(names), 128):
            tokens = tokenizer(names[start:start + 128]).to(device)
            encoded = model.encode_text(tokens)
            encoded = encoded / encoded.norm(dim=-1, keepdim=True).clamp_min(1e-12)
            text_vectors.append(encoded.float().cpu().numpy())
    text_seconds = time.perf_counter() - started
    lookup = dict(zip(names, np.concatenate(text_vectors), strict=True))
    scores = np.array([float(np.dot(vectors[i], lookup[row["entity_name"]]))
                       for i, row in zip(indices, rows, strict=True)])
    actual = np.array([row["keep"] for row in rows], dtype=np.int8)

    # Split by entity, so calibration never sees labels for test entities.
    entities = sorted({str(row["entity_id"]) for row in rows})
    rng = np.random.default_rng(args.seed)
    rng.shuffle(entities)
    calibration_entities = set(entities[:max(1, len(entities) // 2)])
    calibration = np.array([str(row["entity_id"]) in calibration_entities for row in rows])
    test = ~calibration
    if not (calibration.any() and test.any()):
        raise ValueError("Not enough entities for a group split")
    # Maximize balanced accuracy on calibration; use a fixed, untouched test split.
    candidates = np.unique(np.quantile(scores[calibration], np.linspace(0, 1, 201)))
    threshold = max(candidates, key=lambda value: (
        metrics(actual[calibration], (scores[calibration] >= value).astype(np.int8))["balanced_accuracy"],
        -value,
    ))
    test_results = metrics(actual[test], (scores[test] >= threshold).astype(np.int8))
    majority = int(actual[calibration].mean() >= 0.5)
    majority_results = metrics(actual[test], np.full(test.sum(), majority, dtype=np.int8))
    test_dimensions = np.array([row.get("original_dimensions") or [0, 0] for row in rows])[test]
    small = np.min(test_dimensions, axis=1) < 256
    technical_results = metrics(actual[test], (~small).astype(np.int8))

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Team 3 canonical reviewed-sheet labels joined by batch + entity_id + URL to decoded pilot images",
        "model": MODEL_ID, "device": device, "seed": args.seed,
        "valid_labeled_images": len(rows), "class_counts": dict(Counter("keep" if x else "reject" for x in actual)),
        "entities": len(entities), "calibration_images": int(calibration.sum()),
        "test_images": int(test.sum()), "threshold": float(threshold),
        "test_majority_baseline": majority_results,
        "test_min_short_side_256_baseline": technical_results,
        "test_clip_entity_name_filter": test_results,
        "unique_entity_texts": len(names), "text_encode_seconds": round(text_seconds, 3),
        "text_encode_per_second": round(len(names) / text_seconds, 2) if text_seconds else None,
        "limitations": [
            "The pilot samples source-assigned rows; it is not a random sample of all 10-100M crawled images.",
            "Human x combines wrong entity, watermark, duplicates, inaccessible image, and other reasons.",
            "Entity-name CLIP similarity alone cannot verify exact locality, politics, NSFW, or watermark.",
            "Image embedding speed was measured separately in retrieval_pilot_report.json; cached vectors are used here.",
            "Threshold was selected on calibration entities only; this test split is a preliminary holdout, not independent external validation.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
