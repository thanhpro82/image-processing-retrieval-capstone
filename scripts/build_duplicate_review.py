"""Create a local, stratified human-review page for near-duplicate candidates."""
from __future__ import annotations

import argparse
import base64
import html
import json
import random
from collections import defaultdict
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

PROJECT = Path(__file__).resolve().parents[1]


def thumbnail(path: Path) -> str:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((420, 260), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=68, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=PROJECT / "data" / "corpus_v1")
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--output", type=Path,
                        default=PROJECT / "data" / "analysis" / "near_duplicate_review.html")
    parser.add_argument("--per-stratum", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    dataset = args.dataset.resolve()
    candidates_path = (args.candidates or dataset / "near_duplicate_candidates.jsonl").resolve()

    strata: dict[tuple[int, bool], list[dict]] = defaultdict(list)
    with candidates_path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                strata[(row["distance"], bool(row["same_entity"]))].append(row)

    rng = random.Random(args.seed)
    selected = []
    for key in sorted(strata):
        rows = strata[key]
        selected.extend(rng.sample(rows, min(args.per_stratum, len(rows))))

    entities = {}
    with (dataset / "assembled_manifest.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["status"] == "valid_image":
                entities[row["source_url"]] = sorted({
                    link["entity_name"] for link in row.get("entity_links", [])
                    if link.get("entity_name")
                })

    cards = []
    for number, pair in enumerate(selected, 1):
        left = thumbnail(Path(pair["left_path"]))
        right = thumbnail(Path(pair["right_path"]))
        payload = {
            "sample_id": number,
            "distance": pair["distance"],
            "same_entity": pair["same_entity"],
            "left_url": pair["left_url"],
            "right_url": pair["right_url"],
            "left_entities": entities.get(pair["left_url"], []),
            "right_entities": entities.get(pair["right_url"], []),
        }
        data = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
        left_names = html.escape(" · ".join(payload["left_entities"]) or "(không có tên entity)")
        right_names = html.escape(" · ".join(payload["right_entities"]) or "(không có tên entity)")
        cards.append(f"""
        <article class="card" data-pair="{data}">
          <header><b>#{number}</b><span>dHash distance: {pair['distance']}</span>
          <span>cùng entity: {'Có' if pair['same_entity'] else 'Không'}</span></header>
          <div class="images">
            <figure><img loading="lazy" src="{left}" alt="Ảnh trái"><figcaption>{left_names}</figcaption></figure>
            <figure><img loading="lazy" src="{right}" alt="Ảnh phải"><figcaption>{right_names}</figcaption></figure>
          </div>
          <details><summary>Xem URL nguồn</summary><p>{html.escape(pair['left_url'])}</p><p>{html.escape(pair['right_url'])}</p></details>
          <fieldset><legend>Kết luận</legend>
            <label><input type="radio" name="label-{number}" value="duplicate"> Cùng ảnh (resize/crop/watermark)</label>
            <label><input type="radio" name="label-{number}" value="similar_not_duplicate"> Cùng đối tượng/cảnh, ảnh khác</label>
            <label><input type="radio" name="label-{number}" value="different"> Khác nội dung</label>
            <label><input type="radio" name="label-{number}" value="uncertain"> Chưa rõ</label>
          </fieldset>
          <input class="note" type="text" placeholder="Ghi chú ngắn (tuỳ chọn)">
        </article>""")

    strata_summary = ", ".join(
        f"d={distance}, cùng entity={'có' if same else 'không'}: {len(rows)} mẫu"
        for (distance, same), rows in sorted(strata.items())
    )
    document = f"""<!doctype html>
<html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review ảnh gần trùng</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;background:#f4f6f8;color:#17202a;margin:0}}
main{{max-width:1100px;margin:auto;padding:24px}}h1{{margin-bottom:4px}}.sticky{{position:sticky;top:0;background:#fff;padding:12px;border:1px solid #ccd3da;border-radius:10px;z-index:2;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
.card{{background:white;border:1px solid #d8dee5;border-radius:12px;margin:18px 0;padding:16px;box-shadow:0 2px 8px #17202a10}}header{{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:10px}}.images{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}figure{{margin:0;background:#f4f6f8;border-radius:8px;padding:8px}}img{{width:100%;height:260px;object-fit:contain}}figcaption{{font-size:13px;padding:6px}}fieldset{{border:0;padding:12px 0;display:flex;gap:15px;flex-wrap:wrap}}.note{{width:min(600px,95%);padding:9px;border:1px solid #ccd3da;border-radius:6px}}button{{padding:9px 14px;border:0;border-radius:7px;background:#165dff;color:white;font-weight:600;cursor:pointer}}details{{font-size:12px;overflow-wrap:anywhere}}.hint{{color:#46515d}}
</style><main>
<h1>Review ảnh gần trùng</h1><p class="hint">Chọn “Cùng ảnh” chỉ khi đây là cùng một bức ảnh, kể cả khác kích thước, crop hoặc watermark. Sản phẩm cùng loại nhưng ảnh chụp khác nhau cần chọn “Cùng đối tượng/cảnh, ảnh khác”. Mẫu được phân tầng theo khoảng cách dHash và quan hệ entity; không dùng làm ước lượng chính xác nếu chưa có đủ nhãn độc lập.</p>
<div class="sticky"><span id="progress">Đã gắn nhãn: 0/{len(selected)}</span><label>Người review <input id="reviewer" placeholder="Tên"></label><button id="export">Tải nhãn JSONL</button></div>
<p class="hint">Tổng candidate: {sum(map(len, strata.values()))}. Mẫu: {len(selected)}. Phân tầng: {html.escape(strata_summary)}</p>
{''.join(cards)}
</main><script>
const cards=[...document.querySelectorAll('.card')];
function update(){{const n=cards.filter(c=>c.querySelector('input[type=radio]:checked')).length;document.querySelector('#progress').textContent=`Đã gắn nhãn: ${{n}}/{len(selected)}`;}}
document.querySelectorAll('input[type=radio]').forEach(x=>x.addEventListener('change',update));
document.querySelector('#export').addEventListener('click',()=>{{
 const reviewer=document.querySelector('#reviewer').value.trim();
 const rows=cards.map(c=>{{const pair=JSON.parse(c.dataset.pair);pair.label=c.querySelector('input[type=radio]:checked')?.value||'';pair.note=c.querySelector('.note').value.trim();pair.reviewer=reviewer;pair.reviewed_at=new Date().toISOString();return pair;}}).filter(x=>x.label);
 if(!rows.length){{alert('Chưa có nhãn nào để tải.');return;}}
 const blob=new Blob([rows.map(x=>JSON.stringify(x)).join('\\n')+'\\n'],{{type:'application/x-ndjson'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='near_duplicate_review_labels.jsonl';a.click();URL.revokeObjectURL(a.href);
}});
</script></html>"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding="utf-8")
    print(json.dumps({"candidates": sum(map(len, strata.values())), "sampled_pairs": len(selected),
                      "strata": len(strata), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
