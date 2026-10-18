"""Read-only audit of Team 3 final and recheck image decisions.

This does not create a retrieval corpus or assume blank cells were all reviewed.
It records source provenance and the decision distribution for each workbook.
"""

from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
INCOMING = ROOT / "data" / "incoming"
ANALYSIS = ROOT / "data" / "analysis"
MANIFEST = INCOMING / "team3_download_manifest.json"
DB = ANALYSIS / "team3_label_audit.sqlite"
REPORT = ANALYSIS / "team3_label_audit.json"


def clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ANALYSIS.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    db = sqlite3.connect(DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE rows (
        source_id INTEGER, batch INTEGER, source_kind TEXT, filename TEXT,
        sheet_role TEXT, sheet_name TEXT, excel_row INTEGER, stt TEXT,
        entity_id TEXT, entity_name TEXT, url TEXT, decision TEXT,
        reviewer TEXT, note TEXT
    )""")
    summary = []
    for source_id, item in enumerate(manifest["files"]):
        path = INCOMING / item["kind"] / item["filename"]
        book = load_workbook(path, read_only=True, data_only=True)
        sheet_summaries = []
        for sheet in book.worksheets:
            if sheet.title == "Ảnh đã thẩm định":
                role = "reviewed"
            elif sheet.title.startswith("Ảnh ứng viên"):
                role = "candidate"
            else:
                continue
            iterator = sheet.values
            headers = [clean(x) for x in next(iterator)]
            columns = {name: index for index, name in enumerate(headers)}
            required = ["Entity ID", "URL Ảnh", "Giữ/Bỏ", "Người verify"]
            if any(name not in columns for name in required):
                raise ValueError(f"Missing required columns in {path.name} / {sheet.title}")
            stt_column = "STT (Batch)" if role == "reviewed" else "STT Thực thể (Batch)"
            name_column = "Tên Thực thể"
            counts = collections.Counter()
            buffer = []
            for excel_row, values in enumerate(iterator, start=2):
                def get(column: str) -> str:
                    index = columns.get(column)
                    return clean(values[index]) if index is not None and index < len(values) else ""

                url = get("URL Ảnh")
                entity_id = get("Entity ID")
                if not url and not entity_id:
                    continue
                decision = get("Giữ/Bỏ").lower()
                reviewer = get("Người verify")
                if not url or not entity_id:
                    counts["missing_key"] += 1
                if decision == "":
                    counts["blank"] += 1
                elif decision == "x":
                    counts["x"] += 1
                else:
                    counts["other"] += 1
                if reviewer:
                    counts["with_reviewer"] += 1
                if get("Verify note"):
                    counts["with_note"] += 1
                counts["rows"] += 1
                buffer.append((
                    source_id, item["batch"], item["kind"], item["filename"],
                    role, sheet.title, excel_row, get(stt_column), entity_id,
                    get(name_column), url, decision, reviewer, get("Verify note"),
                ))
                if len(buffer) >= 2000:
                    db.executemany("INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buffer)
                    buffer.clear()
            if buffer:
                db.executemany("INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buffer)
            sheet_summaries.append({"role": role, "sheet": sheet.title, **counts})
        book.close()
        db.commit()
        summary.append({"batch": item["batch"], "kind": item["kind"],
                        "filename": item["filename"], "sheets": sheet_summaries})
        print(f"Audited {len(summary)}/{len(manifest['files'])}: {item['filename']}", flush=True)
    db.execute("CREATE INDEX rows_key ON rows(batch, entity_id, url, sheet_role)")
    db.execute("CREATE INDEX rows_source ON rows(source_id)")
    db.commit()
    queries = {
        "all_rows": "SELECT COUNT(*) FROM rows",
        "unique_entity_url_role": "SELECT COUNT(*) FROM (SELECT 1 FROM rows WHERE entity_id<>'' AND url<>'' GROUP BY batch,entity_id,url,sheet_role)",
        "unique_entity_url": "SELECT COUNT(*) FROM (SELECT 1 FROM rows WHERE entity_id<>'' AND url<>'' GROUP BY batch,entity_id,url)",
        "recheck_keys": "SELECT COUNT(*) FROM (SELECT 1 FROM rows WHERE source_kind='recheck' AND entity_id<>'' AND url<>'' GROUP BY batch,entity_id,url,sheet_role)",
        "recheck_keys_not_in_final": "SELECT COUNT(*) FROM (SELECT 1 FROM rows r WHERE r.source_kind='recheck' AND r.entity_id<>'' AND r.url<>'' AND NOT EXISTS (SELECT 1 FROM rows f WHERE f.source_kind='final' AND f.batch=r.batch AND f.entity_id=r.entity_id AND f.url=r.url AND f.sheet_role=r.sheet_role) GROUP BY r.batch,r.entity_id,r.url,r.sheet_role)",
        "conflicting_decisions_same_key": "SELECT COUNT(*) FROM (SELECT 1 FROM rows WHERE entity_id<>'' AND url<>'' GROUP BY batch,entity_id,url,sheet_role HAVING MIN(decision)<>MAX(decision))",
        "different_decisions_within_recheck": "SELECT COUNT(*) FROM (SELECT 1 FROM rows WHERE source_kind='recheck' AND entity_id<>'' AND url<>'' GROUP BY batch,entity_id,url,sheet_role HAVING MIN(decision)<>MAX(decision))",
        "duplicate_rows_same_file_key": "SELECT COUNT(*) FROM (SELECT 1 FROM rows WHERE entity_id<>'' AND url<>'' GROUP BY source_id,entity_id,url,sheet_role HAVING COUNT(*)>1)",
    }
    totals = {key: db.execute(query).fetchone()[0] for key, query in queries.items()}
    totals["by_source_kind_role_decision"] = [
        dict(zip(("kind", "role", "decision", "rows", "with_reviewer"), record))
        for record in db.execute("""SELECT source_kind, sheet_role, decision, COUNT(*),
                SUM(CASE WHEN reviewer<>'' THEN 1 ELSE 0 END)
                FROM rows GROUP BY source_kind,sheet_role,decision
                ORDER BY source_kind,sheet_role,decision""")
    ]
    report = {"scope": "Team 3 final and latest recheck XLSX; labels only",
              "files": summary, "totals": totals}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False, indent=2), flush=True)
    db.close()


if __name__ == "__main__":
    main()
