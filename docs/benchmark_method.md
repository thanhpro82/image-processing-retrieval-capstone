# Benchmark method

All metrics are tied to a named manifest version, code commit, machine, worker count and UTC run time. Raw company rows, URLs and images stay in the local `data/` directory. The report distinguishes measured values from projections.

## Processing A

1. **Network and decode yield:** Count selected URLs, attempted URLs, HTTP/network failures, successfully decoded images and total source bytes. Resume failures only for transient statuses or DNS/timeouts. The full downloader stops when free disk is below 8 GiB or DNS failures become widespread, so a stalled run is not counted as completed throughput.
2. **Exact duplicates:** Hash original HTTP bytes in the downloader. For imported local WebP files, hash the processed bytes in a separate namespace. The assembled audit decodes every chosen file and recomputes its processed checksum. Report both valid URL rows and distinct processed SHA-256 values; the latter determines whether the 80,000-image threshold is met.
3. **Near duplicates:** Compute versioned dHash on each distinct asset, use five disjoint bands to generate all pairs with Hamming distance at most four, then record candidate pairs and clusters. Inspect contact sheets before calling a pair truly duplicate. A candidate count is an upper bound on potential removals, not a precision or recall claim.
4. **Human-label accuracy:** Join only reviewed-sheet keep/`x` labels by `(batch, entity_id, URL)`. Keep candidate-sheet `x` out of the negative class. Split by entity before threshold selection. Record confusion matrix, reject recall, good-image false-reject rate and coverage; select thresholds on calibration/validation and evaluate once on unseen test entities. Ambiguous reasons in notes do not become labels for watermark, safety or locality metrics without manual confirmation.

5. **Throughput and cost:** Record end-to-end URLs/s and valid images/s separately from local decode/hash images/s and model inference images/s. Include p50/p95 per stage, peak RAM/VRAM, storage bytes, CPU/GPU hours and network bytes. Extrapolate to 10M and 100M using explicit assumptions about source availability, parallel workers and storage prices. Never present an extrapolation as a completed run.

The source-title relevance pilot uses only rows explicitly categorized as
wrong entity/location by note keywords plus human-kept rows. It reports both
the <=1% kept-image flag threshold selected on calibration entities and a
fixed 10% human-review queue on unseen entities. Other rejected rows are
excluded; the resulting precision is conditional on that restricted cohort.
The reason categories are provisional because note keywords are not an
independent adjudication. A failed threshold remains disabled in the pipeline.

`scripts/build_processing_decisions.py` reports wall time for the technical
decision gate over every manifest row and cross-tabulates its actions with
human labels when `--labels-csv` is supplied for the pilot. `scripts/project_processing_scale.py`
uses the measured CPU and download pilots for a separate resource projection;
unknown prices stay unset. Neither report measures semantic/safety model
inference, so neither is an end-to-end cost claim.

## Retrieval B

The SQLite baseline indexes one row per distinct chosen image path, with entity names, captions and documented region links. `scripts/benchmark_api.py` waits for at least 80,000 indexed images, warms the API, then makes deterministic single-intent, multi-intent and entity→region multi-hop queries. It records HTTP request-to-JSON latency p50, p95, maximum and count at or above 3 seconds for each class. A concurrent 100-request probe measures requests/s. The same seed and index version allow repeat runs.

Automatically generated test cases use indexed entity names and region links as **metadata proxies**. A proxy hit means a returned row contains the expected name; it does not prove visual relevance. Before final reporting, independently judge a fixed set of returned top-10 images per query type and record the criteria, annotator and disagreements. If family or other multi-hop relations are absent from the data, the API returns an unsupported answer; do not label it a successful grounded retrieval.

The API benchmark excludes browser image download/render time. For user-perceived latency, measure from form submission until thumbnails finish loading as a separate metric. Record Docker image size, build time, startup time and healthcheck readiness independently.
