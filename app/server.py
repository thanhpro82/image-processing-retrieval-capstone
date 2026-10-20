"""Small local search demo over the verified SQLite FTS corpus."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
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
RAW_STOP_WORDS = {"con", "trong"}


def fold(value: str) -> str:
    value = value.lower().replace("đ", "d")
    return "".join(char for char in unicodedata.normalize("NFKD", value)
                   if not unicodedata.combining(char))


def tokens(query: str) -> list[str]:
    original_words = re.findall(r"[^\W_]+", query.lower(), re.UNICODE)
    return [fold(word) for word in original_words
            if len(fold(word)) > 1 and fold(word) not in STOP_WORDS
            and word not in RAW_STOP_WORDS][:16]


def accented_words(query: str) -> set[str]:
    """Retain user-supplied tones when checking an accent-folded FTS hit."""
    words = {word.lower() for word in re.findall(r"[^\W_]+", query, re.UNICODE)}
    return {word for word in words if fold(word) != word and fold(word) not in STOP_WORDS}


def matches_accents(row: sqlite3.Row, required: set[str]) -> bool:
    if not required:
        return True
    original = " ".join(str(row[column] or "") for column in
                        ("entity_names", "caption", "region", "page_title"))
    present = {word.lower() for word in re.findall(r"[^\W_]+", original, re.UNICODE)}
    return required.issubset(present)


def connection() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def find_images(con: sqlite3.Connection, query: str, limit: int, region: str | None = None,
                deadline: float | None = None) -> list[dict]:
    terms = tokens(query)
    if not terms:
        return []
    required_accents = accented_words(query)
    region_filter = "AND images.region = ?" if region else ""
    statement = f"""
        SELECT images.image_path, images.entity_names, images.caption,
               images.region, images.page_title,
               bm25(images_fts, 8.0, 2.0, 1.0, 3.0, 0.5) AS score
        FROM images_fts JOIN images ON images_fts.rowid = images.id
        WHERE images_fts MATCH ? {region_filter} ORDER BY score LIMIT ?
    """
    # A broad OR query can score most of the 89k-row corpus, especially when a
    # place name contains common province words. Try the whole phrase first;
    # if it is absent, back off to short contiguous phrases before one word.
    # This bounds the work without silently pretending the full intent matched.
    phrases = [terms]
    for width in (3, 2):
        if len(terms) <= width:
            continue
        for start in range(len(terms) - width, -1, -1):
            phrases.append(terms[start:start + width])
            if len(phrases) >= 8:
                break
        if len(phrases) >= 8:
            break
    # A single-word fallback for a longer query often returns a different
    # subject ("con vật sống trong rừng" -> any caption mentioning a forest).
    # Return no result when no two-term group is grounded instead.
    rows = []
    seen = set()
    for phrase in phrases:
        if deadline is not None and time.perf_counter() >= deadline:
            break
        expression = " AND ".join(f'"{term}"' for term in phrase)
        if expression in seen:
            continue
        seen.add(expression)
        phrase_accents = {word for word in required_accents if fold(word) in phrase}
        fetch_limit = min(max(limit * 20, 100), 1000) if phrase_accents else limit
        if deadline is not None:
            con.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
        try:
            candidates = con.execute(statement, (expression, region, fetch_limit)
                                     if region else (expression, fetch_limit)).fetchall()
        except sqlite3.OperationalError as exc:
            if deadline is None or "interrupted" not in str(exc).lower():
                raise
            break
        finally:
            if deadline is not None:
                con.set_progress_handler(None, 0)
        rows = [row for row in candidates if matches_accents(row, phrase_accents)][:limit]
        if rows:
            break
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
    deadline = time.perf_counter() + 2.5
    if re.search(r"\b(?:con trai|con gái|vợ|chồng|cha ruột|mẹ ruột|bố của|cha của|mẹ của)\b", query, re.I):
        return {"type": "multi_hop", "query": query, "results": [],
                "reason": "Chưa có dữ liệu quan hệ gia đình để xác minh truy vấn này."}
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
        results = find_images(con, subject, limit, region=region, deadline=deadline)
        return {"type": "multi_hop", "query": query, "resolved_region": region,
                "reference_entity": reference, "results": results,
                "partial": time.perf_counter() >= deadline}

    parts = [part.strip() for part in re.split(r"\s+và\s+", query, flags=re.I) if part.strip()]
    if len(parts) > 1:
        groups = []
        seen = set()
        combined = []
        for part in parts[:4]:
            hits = find_images(con, part, limit, deadline=deadline)
            groups.append({"intent": part, "results": hits})
        for rank in range(limit):
            for group in groups:
                hits = group["results"]
                if rank < len(hits) and hits[rank]["image_url"] not in seen and len(combined) < limit:
                    hit = hits[rank]
                    seen.add(hit["image_url"])
                    combined.append(hit)
        return {"type": "multi_intent", "query": query, "intents": groups,
                "results": combined, "partial": time.perf_counter() >= deadline}
    results = find_images(con, query, limit, deadline=deadline)
    return {"type": "single_intent", "query": query, "results": results,
            "partial": time.perf_counter() >= deadline}


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
        if parsed.path == "/api/processing":
            path = CORPUS / "processing_decisions_report.json"
            if not path.is_file():
                return self.send_json({"error": "processing report missing"}, HTTPStatus.NOT_FOUND)
            report = json.loads(path.read_text(encoding="utf-8"))
            return self.send_json({
                "rows": report["rows"],
                "distinct_valid_processed_sha256": report["distinct_valid_processed_sha256"],
                "action_counts": report["action_counts"],
                "elapsed_seconds": report["elapsed_seconds"],
                "manifest_rows_per_second": report["manifest_rows_per_second"],
                "scope": "local technical checks on a human-approved corpus",
            })
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
