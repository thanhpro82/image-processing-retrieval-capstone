"""Evaluate whether cached image embeddings can screen mixed human labels.

The holdout is grouped by entity and URLs are unique in the pilot. The output is
an experiment, not a production model or a claim about specific reject reasons.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import balanced_accuracy_score, confusion_matrix, roc_auc_score

PROJECT = Path(__file__).resolve().parents[1]


def read_data(pilot: Path, database: Path) -> tuple[np.ndarray, np.ndarray, list[str]]:
    vectors = np.load(pilot / "image_vectors.npy")
    with (pilot / "indexed_manifest.jsonl").open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    if len(vectors) != len(rows):
        raise ValueError("Manifest and vector counts differ")
    with sqlite3.connect(database) as connection:
        labels = {
            (int(batch), str(entity_id), url): decision == "x"
            for batch, entity_id, url, decision in connection.execute(
                "SELECT batch, entity_id, url, decision FROM canonical_role "
                "WHERE sheet_role='reviewed' AND decision IN ('', 'x')"
            )
        }
    matches = []
    for i, row in enumerate(rows):
        key = (int(row["batch"]), str(row["entity_id"]), row["source_url"])
        if key in labels:
            matches.append((i, labels[key], str(row["entity_id"])))
    indices, reject, entities = zip(*matches, strict=True)
    return vectors[list(indices)], np.asarray(reject, dtype=bool), list(entities)


def result(actual_reject: np.ndarray, predicted_reject: np.ndarray) -> dict:
    tn, fp, fn, tp = confusion_matrix(actual_reject, predicted_reject, labels=[False, True]).ravel()
    return {
        "n": len(actual_reject),
        "human_keep": int((~actual_reject).sum()),
        "human_reject": int(actual_reject.sum()),
        "auto_reject": int(predicted_reject.sum()),
        "true_reject": int(tp),
        "false_reject_good": int(fp),
        "reject_recall": round(float(tp / (tp + fn)), 4) if tp + fn else 0.0,
        "good_false_reject_rate": round(float(fp / (fp + tn)), 4) if fp + tn else 0.0,
        "reject_precision": round(float(tp / (tp + fp)), 4) if tp + fp else 0.0,
        "balanced_accuracy": round(float(balanced_accuracy_score(actual_reject, predicted_reject)), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=Path, default=PROJECT / "data" / "pilot")
    parser.add_argument("--database", type=Path, default=PROJECT / "data" / "analysis" / "team3_label_audit.sqlite")
    parser.add_argument("--output", type=Path, default=PROJECT / "data" / "analysis" / "embedding_classifier_report.json")
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    vectors, labels, entities = read_data(args.pilot, args.database)
    unique_entities = sorted(set(entities))
    np.random.default_rng(args.seed).shuffle(unique_entities)
    train_entities = set(unique_entities[:len(unique_entities) // 2])
    validation_entities = set(unique_entities[len(unique_entities) // 2:3 * len(unique_entities) // 4])
    train = np.array([entity in train_entities for entity in entities])
    validation = np.array([entity in validation_entities for entity in entities])
    test = ~(train | validation)
    started = time.perf_counter()
    models = []
    for regularization in (0.01, 0.1, 1.0, 10.0):
        model = LogisticRegression(C=regularization, class_weight="balanced", max_iter=1000)
        model.fit(vectors[train], labels[train])
        probabilities = model.predict_proba(vectors[validation])[:, 1]
        score = roc_auc_score(labels[validation], probabilities)
        models.append((score, regularization, model))
    validation_auc, chosen_c, model = max(models, key=lambda item: item[0])
    fit_seconds = time.perf_counter() - started
    validation_prob = model.predict_proba(vectors[validation])[:, 1]
    test_prob = model.predict_proba(vectors[test])[:, 1]
    test_auc = roc_auc_score(labels[test], test_prob)

    # Select on validation only; if no useful threshold exists, reject nobody.
    candidates = [float("inf"), *np.unique(validation_prob)]
    eligible = []
    for threshold in candidates:
        measured = result(labels[validation], validation_prob >= threshold)
        if measured["good_false_reject_rate"] <= 0.01:
            eligible.append((measured["true_reject"], -measured["false_reject_good"], threshold))
    threshold = max(eligible, key=lambda item: item[:2])[2]
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Team 3 human keep/x on decoded pilot; cached OpenCLIP image embeddings",
        "seed": args.seed,
        "images": len(labels),
        "entities": len(unique_entities),
        "train_images": int(train.sum()),
        "validation_images": int(validation.sum()),
        "test_images": int(test.sum()),
        "candidate_C": [0.01, 0.1, 1.0, 10.0],
        "selected_C": chosen_c,
        "validation_auc": round(float(validation_auc), 4),
        "test_auc": round(float(test_auc), 4),
        "training_seconds_excluding_embedding": round(fit_seconds, 3),
        "threshold_for_at_most_1pct_good_false_reject_on_validation": None if np.isinf(threshold) else threshold,
        "validation_policy": result(labels[validation], validation_prob >= threshold),
        "test_policy": result(labels[test], test_prob >= threshold),
        "test_keep_all_baseline": result(labels[test], np.zeros(int(test.sum()), dtype=bool)),
        "limitations": [
            "Human x combines unrelated reject reasons; it is not a safety or watermark label.",
            "Image embedding time and cost are excluded from the classifier fit time.",
            "A small holdout cannot establish a 1% production error bound.",
            "Threshold and C were selected without test labels; test results must not be tuned repeatedly.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
