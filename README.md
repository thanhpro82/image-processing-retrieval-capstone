# Image Processing & Retrieval Capstone

A project for automated post-crawl image screening, with a small retrieval product to demonstrate the processed corpus. The main research focus is measuring screening quality, throughput, and cost. The final demo targets at least 80,000 real images and supports single-intent, multi-intent, and multi-hop search.

## Status

The local, ignored corpus audit confirms more than 80,000 distinct processed image contents, and the Docker Compose demo indexes the assembled corpus. Full-corpus retrieval timings and processing findings are recorded locally under `data/analysis/` and are excluded from Git because they derive from company data. Pain Point A's semantic and safety filters remain experimental; current automatic decisions cover file integrity and exact duplicates, while dHash pairs go to review.

## Data handling

Input spreadsheets, image URLs, downloaded images, labels, local reports, and credentials are excluded from Git. Supply authorized data locally. Do not place private image data or access keys in a public repository.

## Local setup

Python 3.12+ is recommended for the pilot scripts.

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements-pilot.txt

The scripts in `scripts/` expect authorized input files under `data/` or accept paths as command-line arguments. `scripts/scale_probe.py` samples approved image URLs from a local canonical SQLite database and writes resumable results under `data/scale_probe_10k/`. `scripts/build_corpus.py` then creates a larger content-addressed corpus. It checkpoints every batch, stops if free disk falls below 8 GiB, and pauses when DNS failures affect many sources. Resume temporary failures with `--retry-errors`; permanent HTTP failures remain recorded.

`scripts/near_duplicate_probe.py` computes dHash candidates on either the scale probe or a full corpus (`--dataset path`). Candidate pairs require visual confirmation before removal. `scripts/evaluate_technical_filters.py` and `scripts/evaluate_embedding_classifier.py` measure screening baselines on a labeled pilot with entity-grouped holdouts. Their outputs are local; a classifier is not activated merely because it was evaluated.

For a local visual quality check of full-corpus dHash candidates, run `python scripts/build_duplicate_review.py`. It creates an ignored HTML page with a deterministic stratified sample; choose whether each pair is the same photo, a distinct photo of a similar subject, different, or uncertain, then export labels as JSONL. Candidate pairs alone are never treated as verified duplicates.

If the same authorized Team 3 URL set has already been downloaded locally by the companion retrieval project, install `requirements-import.txt` and run `python scripts/import_local_corpus.py --source PATH_TO_ITS_DATA`. The importer matches canonical URL selections, verifies each WebP, records SHA-256, and creates hard links rather than copying the images. It leaves the source project untouched and records the source transform in `data/corpus_v1/assembled_manifest.jsonl`. It is resumable and does not upload data.

To reproduce the optional geometric duplicate experiment, install
`requirements-duplicate.txt` and run
`python scripts/evaluate_duplicate_geometry.py`. It joins the local 100-pair
human review sample to the dHash candidate manifest, measures SIFT/RANSAC
matches, and calibrates a candidate threshold without using the held-out
URL groups. The report remains under ignored `data/analysis/`. This script
does not merge or remove images; a small single-reviewer sample cannot
establish a production false-merge rate.

After import, run `python scripts/verify_assembled_corpus.py` and inspect `data/corpus_v1/assembled_verification_report.json`. The report must confirm at least 80,000 distinct processed hashes and no invalid files before building the demo index:

    python scripts/build_search_db.py --source PATH_TO_ITS_DATA
    python -m unittest discover -s tests
    docker compose up --build

To reproduce the CPU-side processing measurement on a seeded, uniform sample of
the assembled assets, run:

    python scripts/benchmark_processing.py --sample 10000 --workers 4

The ignored `data/corpus_v1/processing_cpu_benchmark_report.json` records
checksum, decode and dHash latency, measured throughput, and a same-machine
projection. Supply `--cpu-usd-per-hour` only when an actual machine-hour price
is known. This benchmark excludes download, semantic verification, safety
screening, candidate-pair search, and human review; it must not be described as
end-to-end throughput for Pain Point A.

To run the implemented conservative decision gate over every assembled row,
including retries, file quarantine, exact-byte asset reuse and near-duplicate
review routing, run:

    python scripts/build_processing_decisions.py --workers 4

The local `data/corpus_v1/processing_decisions.jsonl` records one action per
manifest row, and `processing_decisions_report.json` records counts and wall
time. `pass_technical_pending_content` means the file passed technical checks;
it is **not** automatic approval of entity relevance, watermark or safety.
These corpus images entered with human keep labels. The decision gate cannot
be scored as an automatic semantic filter on this human-preselected corpus.
For the 1,000-row pilot, the same gate can be run against existing human labels
without re-reviewing images:

    python scripts/build_processing_decisions.py --corpus data/pilot --image-root data/pilot/images --manifest data/pilot/manifest.jsonl --near-pairs data/pilot/no_pairs.jsonl --labels-csv data/analysis/processing_pilot_label_audit.csv --output data/analysis/processing_pilot_gate_decisions.jsonl --report data/analysis/processing_pilot_gate_report.json

The absent `no_pairs.jsonl` intentionally disables the separate near-duplicate
queue for this small pilot; the run measures the technical gate only. Its
human-label cross-tab exposes rejected images that still pass technical checks.

For a disk-backed version with a bounded Python row batch, run
`python scripts/build_processing_decisions_streaming.py --chunk-rows 1000`.
It stores seen paths, exact hashes and near-candidate keys in a temporary
SQLite database, then emits the same per-row decisions as the original gate.
Its output and report use `processing_decisions_streaming.*`. This removes the
full-manifest Python-memory dependency, but SQLite disk state still grows
with the number of unique assets and candidate keys. It is a local prototype,
not a measured 10–100M deployment.

`python scripts/evaluate_source_relevance.py` evaluates a cheap title/entity
overlap score against the pilot's explicit wrong-entity/location notes. The
local report shows both a calibration-only low-false-flag threshold and a
fixed 10% review queue on entity-disjoint test data. This experiment never
changes automated accept/reject actions; the note-derived reason labels are
small and not independently adjudicated.

An optional adult-content model runs in **shadow mode only**. Install the
pilot requirements, then place the pinned Apache-2.0
[Marqo NSFW model](https://huggingface.co/Marqo/nsfw-image-detection-384?not-for-all-audiences=true)
files in ignored `.cache/nsfw_model/` (PowerShell commands):

    mkdir .cache/nsfw_model
    curl.exe -L -C - -o .cache/nsfw_model/config.json https://huggingface.co/Marqo/nsfw-image-detection-384/resolve/0c26ec22111b83f106d72a55f611ec35962bcb65/config.json
    curl.exe -L -C - -o .cache/nsfw_model/model.safetensors https://huggingface.co/Marqo/nsfw-image-detection-384/resolve/0c26ec22111b83f106d72a55f611ec35962bcb65/model.safetensors
    python scripts/pilot_safety_shadow.py --batch-size 16

The script verifies both checksums, runs locally, and writes ignored score and
timing reports under `data/analysis/`. It does not upload images or alter the
processing decisions. The pilot has no independently confirmed NSFW-positive
labels, so its threshold counts and throughput are not accuracy evidence or a
reason to enable automatic rejection.

`python scripts/project_processing_scale.py --safety-report data/analysis/safety_shadow_report.json`
writes a local 1M/10M/100M resource projection from the measured 10,000-image
CPU probe, 1,000-URL download pilot and optional adult-content shadow stage.
Supply `--machine-hour-usd`, `--gpu-hour-usd` and
`--storage-gb-month-usd` only when actual infrastructure rates are known.
Missing rates remain `null`; the projection excludes other models, network and
human review cost and is not a measured end-to-end scale result. The shadow
model has no validated safety accuracy on this corpus.
The proposed sharded design and its evidence boundary are in
[`docs/scale_design.md`](docs/scale_design.md).

If local port 8080 is already in use, set a different host port before starting Compose (PowerShell: `$env:DEMO_PORT=8081`).

Open `http://localhost:8080`. Docker mounts `data/corpus_v1` read-only and copies the SQLite index into a named Linux volume at startup so queries do not scan the Windows bind mount. The container does not put any company images in the public image or Git repository. `/healthz`, `/api/search?q=...`, and `/api/summary` are available for automated checks. The current search method is a SQLite FTS baseline over entity names, captions and region. Its multi-hop support is limited to explicitly recorded entity→region links; unsupported relations return no grounded answer. API timings are available in the local report; independently judged relevance is still needed.

With the demo running, `python scripts/benchmark_api.py` generates deterministic metadata-based cases for all three query types and measures API latency plus a small concurrent load probe. Its proxy hit metric does not replace independent human relevance judgments. The local JSON report and cases stay under `data/corpus_v1/`.

## Remaining work

- Validate semantic, watermark and safety screening against reason-specific labels; do not treat mixed human `x` labels as one target
- Add independent human relevance judgments for retrieval cases and report image-render latency separately
- Measure the model inference, storage and cloud costs needed for a defensible 10–100M extrapolation
- Complete a fresh-machine Docker Compose run and document first-start behavior

See the capstone brief provided with the authorized dataset for exact acceptance criteria.
