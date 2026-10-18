# Image Processing & Retrieval Capstone

A project for automated post-crawl image screening, with a small retrieval product to demonstrate the processed corpus. The main research focus is measuring screening quality, throughput, and cost. The final demo targets at least 80,000 real images and supports single-intent, multi-intent, and multi-hop search.

## Status

Implementation is in progress. The repository currently contains data audit and pilot scripts; results from preliminary pilots are not a claim that the final 80,000-image or latency requirements have been met.

## Data handling

Input spreadsheets, image URLs, downloaded images, labels, local reports, and credentials are excluded from Git. Supply authorized data locally. Do not place private image data or access keys in a public repository.

## Local setup

Python 3.12+ is recommended for the pilot scripts.

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements-pilot.txt

The scripts in `scripts/` expect authorized input files under `data/` or accept paths as command-line arguments. `scripts/scale_probe.py` samples approved image URLs from a local canonical SQLite database and writes resumable results under `data/scale_probe_10k/`. `scripts/build_corpus.py` then creates a larger content-addressed corpus. It checkpoints every batch, stops if free disk falls below 8 GiB, and pauses when DNS failures affect many sources. Resume temporary failures with `--retry-errors`; permanent HTTP failures remain recorded.

`scripts/near_duplicate_probe.py` computes dHash candidates on either the scale probe or a full corpus (`--dataset path`). Candidate pairs require visual confirmation before removal. `scripts/evaluate_technical_filters.py` and `scripts/evaluate_embedding_classifier.py` measure screening baselines on a labeled pilot with entity-grouped holdouts. Their outputs are local; a classifier is not activated merely because it was evaluated.

If the same authorized Team 3 URL set has already been downloaded locally by the companion retrieval project, install `requirements-import.txt` and run `python scripts/import_local_corpus.py --source PATH_TO_ITS_DATA`. The importer matches canonical URL selections, verifies each WebP, records SHA-256, and creates hard links rather than copying the images. It leaves the source project untouched and records the source transform in `data/corpus_v1/assembled_manifest.jsonl`. It is resumable and does not upload data.

## Planned deliverables

- Resumable image screening pipeline with per-stage decisions and human-review queue
- Reproducible evaluation of accuracy, throughput, resource use, and cost
- At least 80,000 real images indexed for retrieval
- Search API and web demo packaged with Docker Compose
- Benchmark and technical documentation

See the capstone brief provided with the authorized dataset for exact acceptance criteria.
