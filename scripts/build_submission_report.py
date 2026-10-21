"""Build a local, aggregate-only capstone performance report from measured JSON.

The report stays under ignored data/ by default. It never copies image rows,
URLs, labels, or machine-local paths into the output.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def validate(audit: dict, gate: dict, cpu: dict, safety: dict, api: dict, scale: dict,
             safety_corpus: dict | None = None, streaming: dict | None = None) -> None:
    if audit["unique_processed_sha256"] < 80_000 or audit["invalid_count"]:
        raise ValueError("Corpus has not passed the >=80,000 distinct valid-image audit")
    if audit["manifest_rows"] != gate["rows"]:
        raise ValueError("Corpus audit and processing gate row counts differ")
    if audit["unique_processed_sha256"] != gate["distinct_valid_processed_sha256"]:
        raise ValueError("Corpus audit and processing gate hash counts differ")
    if audit["unique_existing_paths"] != api["indexed_images"]:
        raise ValueError("Indexed image count differs from audited path count")
    if safety["policy"] != "shadow_only_no_automatic_rejection":
        raise ValueError("Safety model is no longer in the documented shadow policy")
    projected_safety = safety_corpus or safety
    if scale["adult_content_shadow_pilot_images_per_second"] != projected_safety["images_per_second"]:
        raise ValueError("Projection uses a different safety run")
    if safety_corpus is not None:
        if safety_corpus["policy"] != "shadow_only_no_automatic_rejection":
            raise ValueError("Corpus safety probe is not shadow-only")
        if safety_corpus["images"] != scale["adult_content_shadow_pilot_images"]:
            raise ValueError("Projection uses a different safety sample size")
    if scale["cpu_sample_throughput_assets_per_second"] != cpu["throughput_assets_per_second"]:
        raise ValueError("Projection uses a different CPU run")
    if sum(gate["action_counts"].values()) != gate["rows"]:
        raise ValueError("Processing actions do not cover the manifest")
    if streaming is not None and (
        streaming.get("rows") != gate["rows"]
        or streaming.get("distinct_valid_processed_sha256") != gate["distinct_valid_processed_sha256"]
        or streaming.get("action_counts") != gate["action_counts"]
        or streaming.get("execution_mode") != "disk_backed_streaming"
    ):
        raise ValueError("Streaming gate differs from the validated in-memory gate")


def render(audit: dict, gate: dict, cpu: dict, safety: dict, api: dict,
           scale: dict, commit: str, safety_corpus: dict | None = None,
           streaming: dict | None = None) -> str:
    validate(audit, gate, cpu, safety, api, scale, safety_corpus, streaming)
    kinds = (("single_intent", "Một ý định"), ("multi_intent", "Nhiều ý định"),
             ("multi_hop", "Nhiều bước"))
    lines = [
        "# Báo cáo hiệu năng capstone — dữ liệu đo tại máy",
        "",
        f"**Commit:** `{commit}`. **Trọng tâm:** A, xử lý ảnh sau crawl; B là demo truy xuất.",
        "Báo cáo chỉ chứa số liệu tổng hợp, không chứa ảnh, URL hay nhãn từng dòng.",
        "",
        "## Phạm vi dữ liệu",
        "",
        f"- {audit['manifest_rows']:,} URL được chọn từ các bảng con người đã thẩm định; "
        f"{audit['valid_decodable_image_rows']:,} dòng giải mã được.",
        f"- {audit['unique_existing_paths']:,} ảnh được index; "
        f"{audit['unique_processed_sha256']:,} nội dung WebP khác nhau theo SHA-256. "
        f"Audit ghi {audit['invalid_count']} file không hợp lệ.",
        "- Corpus này đã được con người chọn trước. Vì thế các phép đo trên corpus "
        "không chứng minh máy tự chọn đúng ảnh từ crawl thô.",
        "",
        "## A — xử lý ảnh",
        "",
        "| Phép đo thực tế | Cỡ mẫu | Thông lượng | Phạm vi |",
        "|---|---:|---:|---|",
        f"| Đọc/hash/giải mã/dHash CPU | {cpu['sampled_assets']:,} ảnh | "
        f"{cpu['throughput_assets_per_second']:.2f} ảnh/s | {cpu['workers']} worker, ảnh đã tải |",
        f"| Cổng quyết định kỹ thuật | {gate['rows']:,} dòng | "
        f"{gate['manifest_rows_per_second']:.2f} dòng/s | Kiểm file, tái sử dụng SHA, định tuyến nghi trùng |",
        *([f"| Cổng kỹ thuật dạng streaming | {streaming['rows']:,} dòng | "
           f"{streaming['manifest_rows_per_second']:.2f} dòng/s | "
           f"SQLite lưu trạng thái, batch {streaming['chunk_rows']:,} dòng; cùng kết quả quyết định |"]
          if streaming else []),
        f"| Model NSFW shadow | {safety['images']:,} ảnh | "
        f"{safety['images_per_second']:.2f} ảnh/s | {safety['device'].upper()}, batch {safety['batch_size']}, không tự loại |",
        *([f"| Model NSFW shadow trên corpus | {safety_corpus['images']:,} ảnh | "
           f"{safety_corpus['images_per_second']:.2f} ảnh/s | "
           f"{safety_corpus['device'].upper()}, mẫu ngẫu nhiên ảnh khác đường dẫn, không tự loại |"]
          if safety_corpus else []),
        "",
        f"Cổng kỹ thuật: {gate['action_counts'].get('pass_technical_pending_content', 0):,} "
        "qua kiểm tra file nhưng **chưa được máy kiểm nội dung**; "
        f"{gate['action_counts'].get('review_near_duplicate', 0):,} vào hàng nghi trùng; "
        f"{gate['action_counts'].get('reuse_exact_asset', 0):,} dùng lại asset trùng byte; "
        f"{gate['action_counts'].get('retry_or_review_source', 0):,} cần tải lại/xem nguồn.",
        *(["Bản streaming cho cùng số quyết định nhưng chậm hơn bản giữ trạng thái "
           "trong RAM; nó giới hạn kích thước batch Python, còn SQLite vẫn tăng "
           "theo số asset. Đây chưa phải benchmark 10–100 triệu ảnh."] if streaming else []),
        "",
        f"NSFW shadow tại ngưỡng 0,5 gắn cờ "
        f"{safety['flag_counts_by_mixed_human_label'].get('0.5', {}).get('keep', 0)} "
        f"ảnh người giữ trên {safety['human_mixed_label_counts'].get('keep', 0)} "
        "ảnh pilot hợp lệ. Pilot không có nhãn NSFW dương được xác nhận độc lập, "
        "nên **không tính được precision/recall NSFW và không bật auto-reject**. "
        "Nhãn Giữ/Bỏ tổng hợp không phải nhãn của từng lý do.",
        "",
        *([f"Mẫu corpus lớn hơn có "
           f"{sum(safety_corpus['flag_counts_by_mixed_human_label'].get('0.5', {}).values())} "
           f"ảnh được model gắn cờ ở ngưỡng 0,5 trên {safety_corpus['images']:,} ảnh; "
           f"p50/p95/p99 điểm số là "
           f"{safety_corpus['score_quantiles_by_mixed_human_label']['unknown']['p50']:.4f}/"
           f"{safety_corpus['score_quantiles_by_mixed_human_label']['unknown']['p95']:.4f}/"
           f"{safety_corpus['score_quantiles_by_mixed_human_label']['unknown']['p99']:.4f}. "
           "Đây là phân bố điểm số trên tập con người đã giữ, chưa phải false-positive rate "
           "hay accuracy NSFW."] if safety_corpus else []),
        "",
        "## B — truy xuất trên toàn index",
        "",
        f"HTTP GET đến JSON sau warm-up tại `{api.get('base_url', 'địa chỉ không ghi')}`; "
        "không gồm tải và render thumbnail.",
        "",
        "| Loại query | Số request | p50 | p95 | Lớn nhất | ≥3 giây | Lỗi | Một phần |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for kind, label in kinds:
        item = api["by_type"][kind]
        lines.append(f"| {label} | {item['requests']} | {item['p50_ms']:.2f} ms | "
                     f"{item['p95_ms']:.2f} ms | {item['max_ms']:.2f} ms | "
                     f"{item['over_3000ms']} | {item.get('errors', 'chưa ghi')} | "
                     f"{item.get('partial_responses', 'chưa ghi')} |")
    load_probe = api["load_probe"]
    lines += [
        "",
        f"Probe tải: {load_probe['requests']} request, {load_probe['workers']} worker, "
        f"{load_probe['throughput_requests_per_second']:.2f} request/s, "
        f"p95 {load_probe['p95_ms']:.2f} ms, "
        f"{load_probe.get('over_3000ms', 'chưa ghi')} request ≥3 giây, "
        f"{load_probe.get('errors', 'chưa ghi')} lỗi, "
        f"{load_probe.get('partial_responses', 'chưa ghi')} kết quả một phần. "
        "Ca thử sinh từ metadata nên tỷ lệ khớp metadata **không phải** "
        "precision/recall theo nội dung ảnh. Multi-hop chỉ hỗ trợ quan hệ "
        "entity→region đã ghi trong dữ liệu.",
        "",
        "## Ngoại suy công suất — chưa đo ở quy mô 10–100 triệu",
        "",
        "| Số ảnh hợp lệ | Giờ máy CPU | Giờ GPU NSFW shadow | Dung lượng WebP dự kiến |",
        "|---:|---:|---:|---:|",
    ]
    for row in scale["scenarios"]:
        storage_gb = row["processed_storage_bytes_using_cpu_sample_mean"] / 1e9
        lines.append(f"| {row['valid_assets']:,} | "
                     f"{row['cpu_stage_machine_hours_at_sample_throughput']:.2f} | "
                     f"{row['adult_content_shadow_gpu_hours_at_pilot_rate']:.2f} | "
                     f"{storage_gb:.2f} GB |")
    lines += [
        "",
        "Các số trên giả định thông lượng từng công đoạn không đổi; không cộng "
        "thành thời gian end-to-end. Chưa có giá máy/GPU/mạng thực trả hoặc "
        "phép đo đầy đủ cho OCR, watermark, sai thực thể và chính trị, nên "
        "**chưa có tổng chi phí hay bằng chứng chất lượng ở 10–100M ảnh**.",
        "",
        "## Cách chạy lại và nguồn số liệu",
        "",
        "- `python scripts/verify_assembled_corpus.py`: audit ảnh thật và SHA.",
        "- `python scripts/benchmark_processing.py --sample 10000 --workers 4`: CPU.",
        "- `python scripts/build_processing_decisions.py --workers 4`: quyết định kỹ thuật.",
        *(["- `python scripts/build_processing_decisions_streaming.py --workers 4 "
           "--chunk-rows 1000`: bản trạng thái SQLite."] if streaming else []),
        "- `.venv\\Scripts\\python.exe scripts/pilot_safety_shadow.py --batch-size 16`: NSFW shadow pilot.",
        *(["- `.venv\\Scripts\\python.exe scripts/pilot_safety_shadow.py "
           "--manifest data/corpus_v1/assembled_manifest.jsonl --image-root data/corpus_v1 "
           "--no-labels --sample 10000 --seed 20261021 --batch-size 16 "
           "--output data/analysis/safety_corpus_10k_scores.jsonl "
           "--report data/analysis/safety_corpus_10k_report.json`: mẫu corpus 10.000 ảnh."]
          if safety_corpus else []),
        "- `python scripts/benchmark_api.py`: API trên Docker Compose đang chạy.",
        "- `python scripts/project_processing_scale.py --safety-report "
        + ("data/analysis/safety_corpus_10k_report.json" if safety_corpus else
           "data/analysis/safety_shadow_report.json") + "`: ngoại suy từng tầng.",
        "",
        "JSON gốc nằm trong `data/corpus_v1/` và `data/analysis/` trên máy được "
        "ủy quyền. Xem `docs/benchmark_method.md` để biết cách lấy mẫu và giới hạn.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=PROJECT / "data/corpus_v1")
    parser.add_argument("--analysis", type=Path, default=PROJECT / "data/analysis")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--notes-file", type=Path,
                        help="Optional local Markdown note about failed or interrupted runs")
    parser.add_argument("--safety-corpus-report", type=Path,
                        help="Optional larger corpus shadow report used by the scale projection")
    args = parser.parse_args()
    corpus, analysis = args.corpus, args.analysis
    commit = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT, text=True
    ).strip()
    scale = load(analysis / "processing_scale_projection.json")
    corpus_safety_path = args.safety_corpus_report or analysis / "safety_corpus_10k_report.json"
    if args.safety_corpus_report and not corpus_safety_path.is_file():
        raise FileNotFoundError(corpus_safety_path)
    corpus_safety = load(corpus_safety_path) if corpus_safety_path.is_file() else None
    streaming_path = corpus / "processing_decisions_streaming_report.json"
    streaming = load(streaming_path) if streaming_path.is_file() else None
    if (not args.safety_corpus_report and corpus_safety is not None
            and (corpus_safety["images_per_second"] != scale["adult_content_shadow_pilot_images_per_second"]
                 or corpus_safety["images"] != scale["adult_content_shadow_pilot_images"])):
        corpus_safety = None
    body = render(
        load(corpus / "assembled_verification_report.json"),
        load(corpus / "processing_decisions_report.json"),
        load(corpus / "processing_cpu_benchmark_report.json"),
        load(analysis / "safety_shadow_report.json"),
        load(corpus / "api_benchmark_report.json"),
        scale,
        commit,
        corpus_safety,
        streaming,
    )
    notes = args.notes_file or analysis / "benchmark_incidents.md"
    if notes.is_file():
        body += "\n## Sự cố khi đo\n\n" + notes.read_text(encoding="utf-8").strip() + "\n"
    output = args.output or analysis / "submission_performance_report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
