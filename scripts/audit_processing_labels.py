"""Build a local audit table for pilot images and normalize reject-note reasons.

Only canonical labels from the reviewed-image sheet are used as quality labels.
Candidate-sheet decisions are intentionally excluded.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn").replace("đ", "d")
    return " ".join(text.split())


REASON_PATTERNS = {
    "inaccessible_or_unverifiable": re.compile(r"\b(khong tai duoc|loi tai|khong mo duoc|url loi|khong truy cap|khong co noi dung nguon)\b"),
    "duplicate": re.compile(r"\b(trung lap|anh trung|trung nhau|trung byte|duplicate)\b"),
    "wrong_entity_or_location": re.compile(r"\b(sai|khong khop|khong dung|khong lien quan|khong phai|khac entity|khac thuc the|khac dia ban|khac tinh|khac huyen|khac xa|khong the hien dung|khong dung doi tuong|khong phai doi tuong|not the target|not target|different entity)\b"),
    "low_resolution_or_image_quality": re.compile(r"\b(do phan giai|resolution|mo nhoe|anh mo|qua nho|khong ro net|khong ro doi tuong|khong thay ro|chat luong anh|chat luong thap)\b"),
    "watermark_or_text_overlay": re.compile(r"\b(watermark|logo|quang cao|chu de|chu chen|infographic|banner|anh ghep|ghep nhieu khung)\b"),
    "adult_or_sensitive_content": re.compile(r"\b(khieu dam|18\+|noi dung nhay cam|phan dong|chinh tri|bao luc)\b"),
    "insufficient_evidence": re.compile(r"\b(chua du can cu|khong du can cu|khong xac nhan|khong the nhan dien|khong nhan dien duoc|chua ket luan|khong xac minh|chua xac minh|khong dinh danh|chua the xac dinh|khong du hinh|chua du hinh|khong co can cu)\b"),
}


def classify_reason(note: str, label: str) -> tuple[str, str, bool]:
    if label == "keep":
        return "not_applicable_kept", "not_applicable", False
    normalized = fold(note)
    if not normalized:
        return "unclear_missing_note", "unclear", True
    # Verification notes often start with a batch-level summary (including unrelated
    # words such as "duplicate") and put the actual image reason after this marker.
    marker = "| anh:"
    if marker in normalized:
        normalized = normalized.rsplit(marker, 1)[1].strip()
    found = [category for category, pattern in REASON_PATTERNS.items() if pattern.search(normalized)]
    if len(found) == 1:
        return found[0], "heuristic_explicit_note", False
    if len(found) > 1:
        return "multiple_or_mixed_reasons", "heuristic_mixed_note", True
    return "unclear_note_unclassified", "unclassified_note", True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", type=Path, default=PROJECT / "data" / "pilot")
    parser.add_argument("--database", type=Path, default=PROJECT / "data" / "analysis" / "team3_label_audit.sqlite")
    parser.add_argument("--output-csv", type=Path, default=PROJECT / "data" / "analysis" / "processing_pilot_label_audit.csv")
    parser.add_argument("--output-report", type=Path, default=PROJECT / "data" / "analysis" / "processing_pilot_label_audit_report.json")
    parser.add_argument("--review-queue", type=Path, default=PROJECT / "data" / "analysis" / "processing_pilot_reason_review.csv")
    args = parser.parse_args()

    manifest_path = args.pilot / "manifest.jsonl"
    records = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    # The canonical source key includes batch. Stream the reviewed role once and retain
    # only keys present in this 1,000-row pilot.
    wanted = {(int(r["batch"]), str(r.get("entity_id")), r.get("source_url")) for r in records}
    labels: dict[tuple[int, str, str], tuple[str, str, str, str, str, int]] = {}
    with sqlite3.connect(args.database) as con:
        for batch, entity_id, url, decision, reviewer, note, source_kind, filename, excel_row in con.execute(
            "SELECT batch, entity_id, url, decision, reviewer, note, source_kind, filename, excel_row FROM canonical_role "
            "WHERE sheet_role='reviewed' AND decision IN ('', 'x')"
        ):
            key = (int(batch), str(entity_id), url)
            if key in wanted:
                labels[key] = (
                    "keep" if decision == "" else "reject", reviewer or "", note or "",
                    source_kind or "", filename or "", int(excel_row or 0),
                )

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    reason_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    by_batch: dict[str, Counter[str]] = {}
    label_source_counts: Counter[str] = Counter()
    missing_labels = 0
    review_queue = 0
    unclassified_reason_notes = 0
    fields = [
        "batch", "stt", "entity_id", "entity_name", "entity_description", "source_url",
        "source_page", "source_title", "image_path", "image_file",
        "image_status", "human_label", "reviewer", "label_source_kind",
        "label_source_file", "label_source_excel_row", "original_note",
        "normalized_reason", "reason_status", "reason_classification_unresolved",
        "needs_reason_review",
        "eligible_for_model_evaluation",
    ]
    with args.output_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            key = (int(record["batch"]), str(record.get("entity_id")), record.get("source_url"))
            label_info = labels.get(key)
            if label_info:
                label, reviewer, note, source_kind, source_file, source_row = label_info
                reason, reason_status, unresolved_reason = classify_reason(note, label)
                # The keep/reject decision is already human verified in the
                # canonical final/recheck workbook. Unclear prose affects only
                # reason-specific analysis; it does not trigger another image review.
                needs_review = False
            else:
                label, reviewer, note = "unknown", "", ""
                source_kind, source_file, source_row = "", "", 0
                reason, reason_status, needs_review = "missing_canonical_label", "unclear", True
                unresolved_reason = True
                missing_labels += 1
            status = record.get("status", "unknown")
            eligible = label in {"keep", "reject"} and status == "valid_image"
            writer.writerow({
                "batch": record.get("batch"), "stt": record.get("stt"),
                "entity_id": record.get("entity_id"), "entity_name": record.get("entity_name"),
                "entity_description": record.get("description", ""),
                "source_url": record.get("source_url"), "image_path": record.get("image_path", ""),
                "source_page": record.get("source_page", ""), "source_title": record.get("source_title", ""),
                "image_file": str((args.pilot / "images" / record["image_path"]).resolve()) if record.get("image_path") else "",
                "image_status": status, "human_label": label, "reviewer": reviewer,
                "label_source_kind": source_kind, "label_source_file": source_file,
                "label_source_excel_row": source_row,
                "original_note": note, "normalized_reason": reason,
                "reason_status": reason_status,
                "reason_classification_unresolved": str(unresolved_reason).lower(),
                "needs_reason_review": str(needs_review).lower(),
                "eligible_for_model_evaluation": str(eligible).lower(),
            })
            reason_counts[reason] += 1
            label_counts[label] += 1
            status_counts[status] += 1
            label_source_counts[source_kind or "missing"] += 1
            unclassified_reason_notes += int(unresolved_reason)
            batch_counts = by_batch.setdefault(str(record.get("batch")), Counter())
            batch_counts[label] += 1
            if needs_review:
                review_queue += 1

    with args.output_csv.open("r", encoding="utf-8-sig", newline="") as source, args.review_queue.open(
        "w", encoding="utf-8-sig", newline=""
    ) as queue_stream:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(queue_stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row for row in reader if row["needs_reason_review"] == "true")

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "pilot manifest joined to canonical_role by batch + entity_id + URL; human-reviewed final sheet with latest recheck overrides",
        "rows": len(records), "labels": dict(label_counts), "image_status": dict(status_counts),
        "normalized_reasons": dict(reason_counts),
        "label_source_kind": dict(label_source_counts),
        "rows_needing_human_label_resolution": review_queue,
        "rows_needing_reason_review": 0,
        "rows_with_unclassified_reason_note": unclassified_reason_notes,
        "review_queue_csv": str(args.review_queue),
        "missing_canonical_labels": missing_labels,
        "eligible_labeled_valid_images": sum(1 for r in records if r.get("status") == "valid_image" and (int(r["batch"]), str(r.get("entity_id")), r.get("source_url")) in labels),
        "by_batch": {batch: dict(counts) for batch, counts in sorted(by_batch.items(), key=lambda x: int(x[0]))},
        "label_policy": "Binary keep/reject labels are reused directly from human-reviewed final workbook rows; the latest matching recheck row overrides final. No image is re-reviewed for this pilot.",
        "reason_mapping_note": "Reason categories are keyword-derived from human verify notes. Unclear or missing notes remain unclassified and are excluded from reason-specific metrics; they do not block binary keep/reject evaluation.",
    }
    args.output_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
