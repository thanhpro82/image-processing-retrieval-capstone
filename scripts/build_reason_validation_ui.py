"""Build local, offline review pages from the blinded reason-validation CSVs."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

HTML = r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đánh giá ảnh — __REVIEWER__</title>
<style>
:root{font-family:system-ui,"Segoe UI",sans-serif;color:#1b2732;background:#f3f5f7}
*{box-sizing:border-box}body{margin:0}header{background:#152d3d;color:white;padding:18px 24px}
h1{font-size:22px;margin:0 0 5px}header p{margin:0;color:#cfe0e7}main{max-width:1100px;margin:20px auto;padding:0 16px}
.bar,.card,.help{background:white;border:1px solid #dce3e8;border-radius:12px;box-shadow:0 2px 9px #1432460d}
.bar{padding:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.bar span{font-weight:600}
button,.file-button{font:inherit;padding:9px 14px;border-radius:8px;border:1px solid #b8c8d2;background:#fff;cursor:pointer;color:#193448}
button:hover,.file-button:hover{background:#eaf2f6}button:focus-visible,input:focus-visible{outline:3px solid #58a8cd}
.primary{background:#137ea5;color:white;border-color:#137ea5}.primary:hover{background:#0d6687}
.card{margin-top:16px;padding:20px}.entity{font-size:22px;font-weight:700;margin:0 0 8px}.desc{line-height:1.5;color:#384d5b;margin:0 0 14px}
.photo{min-height:250px;background:#ecf0f2;display:flex;align-items:center;justify-content:center;border-radius:8px;overflow:hidden}
.photo img{max-width:100%;max-height:560px;object-fit:contain}.photo .error{padding:20px;color:#a02828}
.choice{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 12px}.choice button{font-weight:600;min-width:138px}
.choice button.active{background:#164e67;color:#fff;border-color:#164e67}
fieldset{border:1px solid #cddae1;border-radius:8px;padding:12px;margin:10px 0}.issues{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px}
label{display:flex;align-items:center;gap:8px}input[type=checkbox]{width:18px;height:18px}.note{width:100%;padding:10px;font:inherit;border:1px solid #bdccd5;border-radius:8px}
.actions{display:flex;gap:8px;justify-content:space-between;flex-wrap:wrap;margin-top:16px}.help{padding:14px;margin-top:16px;line-height:1.5;color:#425767}
#message{min-height:23px;color:#a02828;font-weight:600}progress{width:150px;height:12px}
</style></head><body>
<header><h1>Đánh giá ảnh — __REVIEWER__</h1><p>Chỉ xem ảnh và entity; nhãn/ghi chú cũ đã được ẩn. Trình duyệt không gửi dữ liệu đi đâu.</p></header>
<main><div class="bar"><span id="counter"></span><progress id="progress"></progress><span id="done"></span>
<button id="first-open">Ca chưa chấm</button><button id="export">Xuất CSV</button><button id="backup">Sao lưu JSON</button>
<label class="file-button">Nhập JSON đã lưu<input id="import" type="file" accept="application/json" hidden></label></div>
<section class="card"><div id="entity" class="entity"></div><p id="description" class="desc"></p>
<div class="photo"><img id="image" alt="Ảnh cần đánh giá"><span class="error" id="image-error" hidden>Không mở được ảnh. Chọn “Chưa chắc” và ghi chú.</span></div>
<div class="choice"><button data-choice="keep">Dùng được</button><button data-choice="reject">Có lỗi</button><button data-choice="uncertain">Chưa chắc</button></div>
<fieldset id="issue-box" hidden><legend>Chọn tất cả lỗi thấy rõ</legend><div class="issues">
<label><input type="checkbox" value="wrong_entity">Sai thực thể/địa bàn</label>
<label><input type="checkbox" value="watermark">Watermark/chữ đè cản ảnh</label>
<label><input type="checkbox" value="low_quality">Ảnh mờ/nhỏ/lỗi</label>
<label><input type="checkbox" value="sensitive">Nội dung nhạy cảm</label>
<label><input type="checkbox" value="other">Lỗi khác (ghi chú)</label></div></fieldset>
<input id="note" class="note" placeholder="Ghi chú ngắn nếu cần (không bắt buộc)">
<div id="message" role="status"></div><div class="actions"><button id="previous">← Trước</button>
<button id="save" class="primary">Lưu & ảnh tiếp theo →</button><button id="next">Sau →</button></div></section>
<div class="help">Mỗi ảnh chọn một quyết định. Nếu chọn “Có lỗi”, đánh dấu mọi lỗi nhìn thấy. Có thể dừng giữa chừng; câu trả lời được lưu trong trình duyệt này. Hãy xuất JSON để sao lưu và xuất CSV khi gửi kết quả. Đây là tập kiểm thử, không sửa nhãn gốc.</div></main>
<script>
const cases=__CASES__;const reviewer=__REVIEWER_JSON__;
const storageKey='reason-validation:'+reviewer+':v1';
let answers={};try{answers=JSON.parse(localStorage.getItem(storageKey)||'{}')}catch{answers={}}
let index=0,choice='';const $=id=>document.getElementById(id);
function persist(){try{localStorage.setItem(storageKey,JSON.stringify(answers))}catch{$('message').textContent='Trình duyệt không lưu được; hãy xuất JSON để sao lưu.'}}
function setChoice(value){choice=value;document.querySelectorAll('[data-choice]').forEach(b=>b.classList.toggle('active',b.dataset.choice===value));$('issue-box').hidden=value!=='reject'}
function render(){const row=cases[index],saved=answers[row.case_id]||{};$('counter').textContent=`Ảnh ${index+1}/${cases.length}`;
const count=Object.keys(answers).filter(k=>cases.some(r=>r.case_id===k)).length;$('done').textContent=`Đã chấm ${count}/${cases.length}`;
$('progress').max=cases.length;$('progress').value=count;$('entity').textContent=row.entity_name+' · Batch '+row.batch;
$('description').textContent=row.entity_description||'Không có mô tả';$('image-error').hidden=true;
$('image').hidden=false;$('image').src=row.image_uri;$('note').value=saved.note||'';setChoice(saved.choice||'');
document.querySelectorAll('#issue-box input').forEach(c=>c.checked=(saved.issues||[]).includes(c.value));$('message').textContent=''}
function save(){const row=cases[index];if(!choice){$('message').textContent='Hãy chọn một quyết định.';return false}
const issues=[...document.querySelectorAll('#issue-box input:checked')].map(c=>c.value);
if(choice==='reject'&&!issues.length){$('message').textContent='Hãy chọn ít nhất một lý do hoặc “Lỗi khác”.';return false}
if(choice==='reject'&&issues.includes('other')&&!$('note').value.trim()){$('message').textContent='Lỗi khác cần ghi chú ngắn.';return false}
answers[row.case_id]={choice,issues:choice==='reject'?issues:[],note:$('note').value.trim()};persist();return true}
function download(name,content,type){const blob=new Blob([content],{type});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
function csvCell(value){return '"'+String(value??'').replaceAll('"','""')+'"'}
function exportCsv(){const fields=['case_id','batch','entity_name','entity_description','image_file','matches_entity','watermark_or_obstructive_text','low_quality','adult_or_sensitive_content','uncertain_or_unreadable','review_note'];
const rows=cases.map(row=>{const a=answers[row.case_id]||{};let result={...row,review_note:a.note||''};
if(a.choice==='keep')Object.assign(result,{matches_entity:'yes',watermark_or_obstructive_text:'no',low_quality:'no',adult_or_sensitive_content:'no',uncertain_or_unreadable:'no'});
if(a.choice==='uncertain')Object.assign(result,{matches_entity:'uncertain',watermark_or_obstructive_text:'uncertain',low_quality:'uncertain',adult_or_sensitive_content:'uncertain',uncertain_or_unreadable:'yes'});
if(a.choice==='reject'){const s=new Set(a.issues||[]);Object.assign(result,{matches_entity:s.has('wrong_entity')?'no':'yes',watermark_or_obstructive_text:s.has('watermark')?'yes':'no',low_quality:s.has('low_quality')?'yes':'no',adult_or_sensitive_content:s.has('sensitive')?'yes':'no',uncertain_or_unreadable:'no'})}
return fields.map(k=>csvCell(result[k]||''))});
download(reviewer+'_answers.csv','\ufeff'+[fields.map(csvCell),...rows].map(r=>r.join(',')).join('\r\n'),'text/csv;charset=utf-8')}
document.querySelectorAll('[data-choice]').forEach(b=>b.onclick=()=>setChoice(b.dataset.choice));
$('save').onclick=()=>{if(save()){index=Math.min(index+1,cases.length-1);render()}};
$('previous').onclick=()=>{index=Math.max(0,index-1);render()};$('next').onclick=()=>{index=Math.min(cases.length-1,index+1);render()};
$('first-open').onclick=()=>{const found=cases.findIndex(r=>!answers[r.case_id]);if(found>=0)index=found;render()};
$('export').onclick=exportCsv;
$('backup').onclick=()=>download(reviewer+'_backup.json',JSON.stringify({reviewer,answers},null,2),'application/json');
$('import').onchange=async event=>{const file=event.target.files[0];if(!file)return;try{const data=JSON.parse(await file.text());if(data.reviewer!==reviewer||typeof data.answers!=='object')throw Error('Sai file reviewer');answers=data.answers;persist();render()}catch(e){$('message').textContent='Không nhập được JSON: '+e.message}};
$('image').onerror=()=>{$('image').hidden=true;$('image-error').hidden=false};render();
</script></body></html>'''


def build(source: Path, output: Path) -> int:
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"No review rows: {source}")
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate case_id in review file")
    cases = [{
        "case_id": row["case_id"], "batch": row["batch"],
        "entity_name": row["entity_name"],
        "entity_description": row["entity_description"],
        "image_file": row["image_file"],
        "image_uri": Path(row["image_file"]).resolve().as_uri(),
    } for row in rows]
    reviewer = source.stem
    body = (HTML.replace("__CASES__", json.dumps(cases, ensure_ascii=False).replace("<", "\\u003c"))
            .replace("__REVIEWER_JSON__", json.dumps(reviewer))
            .replace("__REVIEWER__", reviewer))
    output.write_text(body, encoding="utf-8")
    return len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path,
                        default=PROJECT / "data/analysis/reason_validation_pilot")
    args = parser.parse_args()
    for reviewer in ("reviewer_1", "reviewer_2"):
        source = args.dir / f"{reviewer}.csv"
        output = args.dir / f"{reviewer}.html"
        print(f"{output}: {build(source, output)} cases")


if __name__ == "__main__":
    main()
