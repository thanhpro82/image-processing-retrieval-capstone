"""Build a small SQLite FTS index over the verified local image corpus.

This index is a transparent lexical retrieval baseline. It does not claim that
text matching alone solves arbitrary multi-hop questions or visual relevance.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]


def plain(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [plain(item) for item in value if plain(item)]


def fold(value: str) -> str:
    value = value.lower().replace("đ", "d")
    return "".join(char for char in unicodedata.normalize("NFKD", value)
                   if not unicodedata.combining(char))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=PROJECT / "data" / "corpus_v1")
    parser.add_argument("--source", type=Path, required=True,
                        help="The matching local retrieval project Data directory")
    args = parser.parse_args()
    corpus = args.corpus.resolve()
    source = args.source.resolve()
    report_path = corpus / "assembled_verification_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report["invalid_count"] or not report["meets_80000_unique_processed_images"]:
        raise ValueError("Corpus must pass the 80,000-image integrity audit first")

    metadata = pd.read_parquet(source / "manifest.parquet", columns=[
        "url", "entity_names", "caption", "page_title", "region", "branch", "entity_desc",
    ]).drop_duplicates("url").set_index("url")
    by_path = {}
    with (corpus / "assembled_manifest.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["status"] != "valid_image":
                continue
            path = row["image_path"]
            entry = by_path.setdefault(path, {"url": row["source_url"], "names": set()})
            entry["names"].update(link["entity_name"] for link in row.get("entity_links", [])
                                  if link.get("entity_name"))

    destination = corpus / "search.sqlite"
    temp = destination.with_suffix(".sqlite.tmp")
    if temp.exists():
        temp.unlink()
    con = sqlite3.connect(temp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript("""
        CREATE TABLE images (
            id INTEGER PRIMARY KEY,
            image_path TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            entity_names TEXT NOT NULL,
            caption TEXT,
            page_title TEXT,
            region TEXT,
            branch TEXT,
            entity_desc TEXT,
            search_text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE images_fts USING fts5(search_text, content='images', content_rowid='id');
        CREATE TABLE entity_region (entity_name TEXT NOT NULL, folded_name TEXT NOT NULL,
                                    region TEXT NOT NULL, folded_region TEXT NOT NULL,
                                    PRIMARY KEY(folded_name, folded_region));
        CREATE INDEX idx_entity_region_name ON entity_region(folded_name);
    """)
    relations = set()
    for n, (path, entry) in enumerate(by_path.items(), 1):
        url = entry["url"]
        meta = metadata.loc[url] if url in metadata.index else None
        names = set(entry["names"])
        if meta is not None:
            names.update(strings(meta["entity_names"]))
        caption = plain(meta["caption"]) if meta is not None else ""
        page_title = plain(meta["page_title"]) if meta is not None else ""
        region = plain(meta["region"]) if meta is not None else ""
        branch = plain(meta["branch"]) if meta is not None else ""
        entity_desc = plain(meta["entity_desc"]) if meta is not None else ""
        name_text = " | ".join(sorted(names))
        search_text = fold(" ".join((name_text, caption, page_title, region, branch, entity_desc)))
        con.execute("INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (n, path, url, name_text, caption, page_title, region, branch, entity_desc, search_text))
        con.execute("INSERT INTO images_fts(rowid,search_text) VALUES (?,?)", (n, search_text))
        if region:
            relations.update((name, fold(name), region, fold(region)) for name in names)
        if n % 10000 == 0:
            con.commit()
            print(f"indexed={n}/{len(by_path)}", flush=True)
    con.executemany("INSERT OR IGNORE INTO entity_region VALUES (?,?,?,?)", relations)
    con.commit()
    count = con.execute("SELECT count(*) FROM images").fetchone()[0]
    relation_count = con.execute("SELECT count(*) FROM entity_region").fetchone()[0]
    con.close()
    os.replace(temp, destination)
    print(json.dumps({"images_indexed": count, "entity_region_links": relation_count,
                      "database_bytes": destination.stat().st_size}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
