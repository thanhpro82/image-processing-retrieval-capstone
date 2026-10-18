from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import faiss
import numpy as np
import open_clip
import torch
from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
PILOT = PROJECT / "data" / "pilot"
MODEL_ID = "hf-hub:laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
TOP_K = 10
BATCH_SIZE = 48


def chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield start, items[start : start + size]


def normalized(tensor: torch.Tensor) -> np.ndarray:
    tensor = tensor / tensor.norm(dim=-1, keepdim=True).clamp_min(1e-12)
    return tensor.float().cpu().numpy().astype("float32", copy=False)


def summarize_hits(
    query_rows: list[dict], candidate_rows: list[dict], indices: np.ndarray, k: int
) -> dict:
    hits = []
    reciprocal_ranks = []
    for row, ranked in zip(query_rows, indices, strict=True):
        expected = row.get("entity_id")
        rank = next(
            (position for position, candidate in enumerate(ranked[:k], 1)
             if candidate >= 0 and candidate_rows[int(candidate)].get("entity_id") == expected),
            None,
        )
        hits.append(rank is not None)
        reciprocal_ranks.append(1.0 / rank if rank is not None else 0.0)
    return {
        f"hit_rate_at_{k}": round(sum(hits) / len(hits), 4) if hits else None,
        "mrr_at_k": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if query_rows else None,
        "queries": len(query_rows),
    }


def encode_texts(model, tokenizer, texts: list[str], device: str) -> np.ndarray:
    encoded = []
    with torch.inference_mode():
        for _, batch in chunks(texts, BATCH_SIZE):
            tokens = tokenizer(batch).to(device)
            encoded.append(normalized(model.encode_text(tokens)))
    return np.concatenate(encoded, axis=0)


def main() -> None:
    manifest_path = PILOT / "manifest.jsonl"
    rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    rows = [row for row in rows if row.get("status") == "valid_image" and not row.get("duplicate_of")]
    device = "cuda" if torch.cuda.is_available() else "cpu"
    torch.backends.cuda.matmul.allow_tf32 = True
    model, preprocess = open_clip.create_model_from_pretrained(MODEL_ID)
    tokenizer = open_clip.get_tokenizer(MODEL_ID)
    model = model.eval().to(device)

    image_vectors = []
    start_encode = time.perf_counter()
    with torch.inference_mode():
        for start, batch in chunks(rows, BATCH_SIZE):
            tensors = []
            for row in batch:
                path = PILOT / "images" / row["image_path"]
                with Image.open(path) as image:
                    tensors.append(preprocess(image.convert("RGB")))
            pixels = torch.stack(tensors).to(device)
            image_vectors.append(normalized(model.encode_image(pixels)))
            if start % 240 == 0:
                print(f"encoded images {min(start + len(batch), len(rows))}/{len(rows)}", flush=True)
    image_sec = time.perf_counter() - start_encode
    image_matrix = np.concatenate(image_vectors, axis=0)
    dimension = int(image_matrix.shape[1])

    index = faiss.IndexFlatIP(dimension)
    index.add(image_matrix)
    faiss.write_index(index, str(PILOT / "image_index.faiss"))
    np.save(PILOT / "image_vectors.npy", image_matrix)
    (PILOT / "indexed_manifest.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    tokenizer_eval = tokenizer
    eval_rows = [row for row in rows if row.get("entity_name") and row.get("entity_id")]
    plain_texts = [str(row["entity_name"]) for row in eval_rows]
    prompted_texts = [f"a photo of {name}" for name in plain_texts]
    eval_metrics = {}
    query_vectors_by_template = {}
    for label, texts in [("entity_name", plain_texts), ("photo_of_entity_name", prompted_texts)]:
        text_vectors = encode_texts(model, tokenizer_eval, texts, device)
        query_vectors_by_template[label] = text_vectors
        distances, neighbors = index.search(text_vectors, TOP_K)
        eval_metrics[label] = summarize_hits(eval_rows, rows, neighbors, TOP_K)
        eval_metrics[label]["mean_cosine_score_top1"] = round(float(distances[:, 0].mean()), 4)

    chosen_template = max(
        eval_metrics,
        key=lambda label: (eval_metrics[label]["hit_rate_at_10"], eval_metrics[label]["mrr_at_k"]),
    )
    query_vectors = query_vectors_by_template[chosen_template]
    rng = random.Random(20261017)
    query_ids = rng.sample(range(len(eval_rows)), min(80, len(eval_rows)))
    query_texts = [plain_texts[i] if chosen_template == "entity_name" else prompted_texts[i] for i in query_ids]

    def encode_one(query: str) -> np.ndarray:
        with torch.inference_mode():
            token = tokenizer([query]).to(device)
            return normalized(model.encode_text(token))

    # Warm up the model and GPU before collecting latency samples.
    for query in query_texts[:5]:
        query_vec = encode_one(query)
        index.search(query_vec, TOP_K)

    latency_985 = []
    latency_80k = []
    search_985 = []
    search_80k = []
    # Duplicate vectors only to benchmark the exact-index scan at 80k; this is not a dataset/index deliverable.
    tiled = np.tile(image_matrix, (int(np.ceil(80000 / len(image_matrix))), 1))[:80000].copy()
    index_80k = faiss.IndexFlatIP(dimension)
    index_80k.add(tiled)
    del tiled
    for query in query_texts:
        started = time.perf_counter()
        query_vec = encode_one(query)
        encoded_at = time.perf_counter()
        index.search(query_vec, TOP_K)
        searched_at = time.perf_counter()
        index_80k.search(query_vec, TOP_K)
        scaled_at = time.perf_counter()
        latency_985.append(searched_at - started)
        latency_80k.append(scaled_at - started)
        search_985.append(searched_at - encoded_at)
        search_80k.append(scaled_at - searched_at)

    def percentiles(values: list[float]) -> dict:
        ordered = sorted(values)
        return {
            "p50_ms": round(float(np.percentile(ordered, 50) * 1000), 2),
            "p95_ms": round(float(np.percentile(ordered, 95) * 1000), 2),
            "max_ms": round(max(ordered) * 1000, 2),
        }

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_id": MODEL_ID,
        "model_revision": "1a25a446712ba5ee05982a381eed697ef9b435cf",
        "device": device,
        "embedding_dimension": dimension,
        "similarity": "cosine similarity via normalized vectors and FAISS IndexFlatIP",
        "images_indexed": len(rows),
        "image_encoding_seconds": round(image_sec, 3),
        "image_encoding_images_per_second": round(len(rows) / image_sec, 3),
        "vector_bytes_fp32": int(image_matrix.nbytes),
        "entity_name_proxy_evaluation": eval_metrics,
        "latency_queries": len(query_texts),
        "latency_template": chosen_template,
        "latency_includes": "warm model text encoding plus exact FAISS top-10 search; excludes HTTP, image rendering, model startup, and query parsing",
        "latency_985": {
            "total": percentiles(latency_985),
            "faiss_search_only": percentiles(search_985),
        },
        "latency_synthetic_80000": {
            "total": percentiles(latency_80k),
            "faiss_search_only": percentiles(search_80k),
            "note": "Uses repeated pilot vectors to measure index scan cost; not a quality evaluation on 80,000 real images.",
        },
        "limitations": [
            "Entity-name hit rate is a weak proxy, not a human relevance judgment.",
            "Vietnamese text performance and all three required query types still need proper test cases.",
            "The synthetic 80k index measures latency only, not retrieval quality at corpus scale.",
        ],
    }
    (PILOT / "retrieval_metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
