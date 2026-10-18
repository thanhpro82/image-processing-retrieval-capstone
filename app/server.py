"""Small local search demo over the verified SQLite FTS corpus."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(os.environ.get("CORPUS_DIR", ROOT / "data" / "corpus_v1")).resolve()
DB = Path(os.environ.get("SEARCH_DB", CORPUS / "search.sqlite")).resolve()
STOP_WORDS = {"anh", "hinh", "cho", "toi", "nhung", "cac", "cua", "ve", "mot", "o", "tai", "la"}


def fold(value: str) -> str:
    value = value.lower().replace("đ", "d")
    return "".join(char for char in unicodedata.normalize("NFKD", value)
                   if not unicodedata.combining(char))


def tokens(query: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", fold(query))
            if len(word) > 1 and word not in STOP_WORDS][:16]


def connection() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def find_images(con: sqlite3.Connection, query: str, limit: int, region: str | None = None) -> list[dict]:
    terms = tokens(query)
    if not terms:
        return []
    expression = " OR ".join(f'"{term}"' for term in terms)
    region_filter = "AND images.region = ?" if region else ""
    rows = con.execute(f"""
        SELECT images.image_path, images.entity_names, images.caption,
               images.region, images.page_title,
               bm25(images_fts, 8.0, 2.0, 1.0, 3.0, 0.5) AS score
        FROM images_fts JOIN images ON images_fts.rowid = images.id
        WHERE images_fts MATCH ? {region_filter} ORDER BY score LIMIT ?
    """, (expression, region, limit) if region else (expression, limit)).fetchall()
    return [{
        "image_url": "/images/" + quote(row["image_path"].replace("\\", "/").removeprefix("images/")),
        "entity_names": row["entity_names"],
        "caption": row["caption"],
        "region": row["region"],
        "page_title": row["page_title"],
        "score": round(float(row["score"]), 4),
    } for row in rows]


def search(con: sqlite3.Connection, query: str, limit: int = 10) -> dict:
    query = query.strip()[:300]
    limit = max(1, min(limit, 50))
    hop = re.search(r"(.+?)\s+ở cùng (?:tỉnh|địa bàn|vùng) với\s+(.+)", query, re.I)
    if hop:
        subject, reference = hop.group(1).strip(), hop.group(2).strip()
        reference = re.sub(r"^(ảnh|hình ảnh)\s+", "", reference, flags=re.I)
        regions = con.execute("""
            SELECT region FROM entity_region WHERE folded_name = ? GROUP BY region LIMIT 3
        """, (fold(reference),)).fetchall()
        if not regions:
            regions = con.execute("""
                SELECT region FROM entity_region WHERE folded_name LIKE ? GROUP BY region LIMIT 3
            """, ("%" + fold(reference) + "%",)).fetchall()
        if not regions:
            return {"type": "multi_hop", "query": query, "results": [],
                    "reason": "Không tìm thấy quan hệ địa bàn có nguồn cho thực thể tham chiếu."}
        if len(regions) > 1:
            return {"type": "multi_hop", "query": query, "results": [],
                    "reason": "Tên thực thể tham chiếu gắn với nhiều địa bàn; cần chỉ rõ địa bàn.",
                    "candidate_regions": [row["region"] for row in regions]}
        region = regions[0]["region"]
        results = find_images(con, subject, limit, region=region)
        return {"type": "multi_hop", "query": query, "resolved_region": region,
                "reference_entity": reference, "results": results}

    parts = [part.strip() for part in re.split(r"\s+và\s+", query, flags=re.I) if part.strip()]
    if len(parts) > 1:
        groups = []
        seen = set()
        combined = []
        for part in parts[:4]:
            hits = find_images(con, part, limit)
            groups.append({"intent": part, "results": hits})
        for rank in range(limit):
            for group in groups:
                hits = group["results"]
                if rank < len(hits) and hits[rank]["image_url"] not in seen and len(combined) < limit:
                    hit = hits[rank]
                    seen.add(hit["image_url"])
                    combined.append(hit)
        return {"type": "multi_intent", "query": query, "intents": groups, "results": combined}
    return {"type": "single_intent", "query": query, "results": find_images(con, query, limit)}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            if not DB.is_file():
                return self.send_json({"status": "index_missing"}, HTTPStatus.SERVICE_UNAVAILABLE)
            with closing(connection()) as con:
                count = con.execute("SELECT count(*) FROM images").fetchone()[0]
            return self.send_json({"status": "ok", "indexed_images": count})
        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            if not query.strip():
                return self.send_json({"error": "q is required"}, HTTPStatus.BAD_REQUEST)
            try:
                limit = int(params.get("limit", ["10"])[0])
                with closing(connection()) as con:
                    output = search(con, query, limit)
            except (ValueError, sqlite3.Error) as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return self.send_json(output)
        if parsed.path == "/api/summary":
            path = CORPUS / "assembled_verification_report.json"
            if not path.is_file():
                return self.send_json({"error": "report missing"}, HTTPStatus.NOT_FOUND)
            return self.send_json(json.loads(path.read_text(encoding="utf-8")))
        if parsed.path.startswith("/images/"):
            relative = unquote(parsed.path[len("/images/"):])
            image_root = (CORPUS / "images").resolve()
            path = (image_root / relative).resolve()
            if not path.is_relative_to(image_root) or not path.is_file() or path.suffix.lower() != ".webp":
                return self.send_error(HTTPStatus.NOT_FOUND)
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in ("/", "/index.html"):
            body = (ROOT / "app" / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    if not DB.is_file():
        raise SystemExit(f"Search index missing: {DB}")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    print(f"Serving on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
