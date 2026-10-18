"""Resolve Team 3 label versions into an auditable metadata view.

The latest recheck replaces the matching final row. Within final files, the
later supplementary workbook replaces an earlier decision for the same exact
(batch, entity ID, URL, sheet role) key. Cross-role decisions remain visible.
No image bytes are downloaded or published here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "analysis" / "team3_label_audit.sqlite"
REPORT = ROOT / "data" / "analysis" / "team3_canonical_report.json"


def scalar(db: sqlite3.Connection, query: str) -> int:
    return int(db.execute(query).fetchone()[0])


def main() -> None:
    db = sqlite3.connect(DB)
    for table, source_filter in [
        ("canonical_final_role", "source_kind='final'"),
        ("canonical_role", "1=1"),
    ]:
        db.execute(f"DROP TABLE IF EXISTS {table}")
        db.execute(f"""CREATE TABLE {table} AS
            SELECT source_id,batch,source_kind,filename,sheet_role,sheet_name,
                   excel_row,stt,entity_id,entity_name,url,decision,reviewer,note
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY batch,entity_id,url,sheet_role
                    ORDER BY source_id DESC
                ) AS version_rank
                FROM rows
                WHERE {source_filter} AND entity_id<>'' AND url<>''
            ) WHERE version_rank=1""")
        db.execute(f"CREATE INDEX {table}_key ON {table}(batch,entity_id,url,sheet_role)")
        db.commit()
        print(f"Built {table}", flush=True)

    report = {
        "scope": "Team 3 image verification batches 1-8; final plus latest recheck",
        "key": ["batch", "Entity ID", "exact URL Ảnh", "sheet role"],
        "version_rule": "Latest recheck wins; within final, later supplementary file wins. Original files are unchanged.",
        "counts": {
            "source_rows": scalar(db, "SELECT COUNT(*) FROM rows"),
            "canonical_role_rows": scalar(db, "SELECT COUNT(*) FROM canonical_role"),
            "recheck_overrides": scalar(db, """SELECT COUNT(*) FROM canonical_role c
                WHERE c.source_kind='recheck'"""),
            "final_only_role_rows": scalar(db, """SELECT COUNT(*) FROM canonical_role c
                WHERE c.source_kind='final'"""),
            "selected_role_rows": scalar(db, "SELECT COUNT(*) FROM canonical_role WHERE decision=''"),
            "rejected_role_rows": scalar(db, "SELECT COUNT(*) FROM canonical_role WHERE decision='x'"),
            "unexpected_decision_rows": scalar(db, "SELECT COUNT(*) FROM canonical_role WHERE decision NOT IN ('','x')"),
            "selected_role_rows_without_reviewer": scalar(db, "SELECT COUNT(*) FROM canonical_role WHERE decision='' AND reviewer=''"),
            "entity_image_links_selected": scalar(db, """SELECT COUNT(*) FROM (
                SELECT 1 FROM canonical_role WHERE decision=''
                GROUP BY batch,entity_id,url)"""),
            "distinct_urls_selected": scalar(db, "SELECT COUNT(DISTINCT url) FROM canonical_role WHERE decision=''"),
            "distinct_entities_with_selected_image": scalar(db, """SELECT COUNT(*) FROM (
                SELECT 1 FROM canonical_role WHERE decision=''
                GROUP BY batch,entity_id)"""),
            "entity_images_in_both_roles": scalar(db, """SELECT COUNT(*) FROM (
                SELECT 1 FROM canonical_role GROUP BY batch,entity_id,url
                HAVING COUNT(*)=2)"""),
            "cross_role_decision_disagreements": scalar(db, """SELECT COUNT(*) FROM (
                SELECT 1 FROM canonical_role GROUP BY batch,entity_id,url
                HAVING MIN(decision)!=MAX(decision))"""),
            "final_selected_entity_image_links": scalar(db, """SELECT COUNT(*) FROM (
                SELECT 1 FROM canonical_final_role WHERE decision=''
                GROUP BY batch,entity_id,url)"""),
            "recheck_decision_changes": scalar(db, """SELECT COUNT(*)
                FROM canonical_final_role f JOIN canonical_role c
                ON c.batch=f.batch AND c.entity_id=f.entity_id AND c.url=f.url
                   AND c.sheet_role=f.sheet_role
                WHERE c.source_kind='recheck' AND c.decision!=f.decision"""),
        },
        "by_batch": [],
    }
    report["cross_role_decisions"] = [
        {"reviewed": reviewed, "candidate": candidate, "entity_image_links": count}
        for reviewed, candidate, count in db.execute("""SELECT r.decision,c.decision,COUNT(*)
            FROM canonical_role r JOIN canonical_role c
            ON c.batch=r.batch AND c.entity_id=r.entity_id AND c.url=r.url
            WHERE r.sheet_role='reviewed' AND c.sheet_role='candidate'
            GROUP BY r.decision,c.decision""")
    ]
    for batch in range(1, 9):
        report["by_batch"].append({
            "batch": batch,
            "selected_entity_image_links": db.execute("""SELECT COUNT(*) FROM (
                SELECT 1 FROM canonical_role WHERE batch=? AND decision=''
                GROUP BY entity_id,url)""", (batch,)).fetchone()[0],
            "distinct_selected_urls": db.execute(
                "SELECT COUNT(DISTINCT url) FROM canonical_role WHERE batch=? AND decision=''", (batch,)
            ).fetchone()[0],
            "selected_role_rows_without_reviewer": db.execute(
                "SELECT COUNT(*) FROM canonical_role WHERE batch=? AND decision='' AND reviewer=''", (batch,)
            ).fetchone()[0],
        })
    report["notes"] = [
        "A blank Giữ/Bỏ is treated as selected per the team rule; this is a label count, not a count of downloadable or distinct image bytes.",
        "The same entity URL may appear in reviewed and candidate sheets. A link is selected if at least one canonical sheet-role row is selected; role disagreements are reported separately.",
        "Exact URL matching may miss semantically identical URLs or near-duplicate image content.",
        "Rows without reviewer and conflicting notes require quality review before being called fully verified.",
    ]
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2), flush=True)
    db.close()


if __name__ == "__main__":
    main()
