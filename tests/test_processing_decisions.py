import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.build_processing_decisions import run
from scripts.build_processing_decisions_streaming import run as run_streaming


class ProcessingDecisionTests(unittest.TestCase):
    def test_exact_reuse_near_candidate_and_invalid_source_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            corpus = Path(folder)
            (corpus / "images").mkdir()
            Image.new("RGB", (20, 16), "red").save(corpus / "images" / "a.webp")
            Image.new("RGB", (20, 16), "blue").save(corpus / "images" / "b.webp")
            Image.new("RGB", (20, 16), "green").save(corpus / "images" / "c.webp")
            (corpus / "images" / "duplicate.webp").write_bytes(
                (corpus / "images" / "a.webp").read_bytes())
            manifest = corpus / "manifest.jsonl"
            rows = [
                {"source_url": "a", "status": "valid_image", "image_path": "images/a.webp",
                 "processed_dimensions": [20, 16], "source_sha256": "a", "human_label": "keep"},
                {"source_url": "dup", "status": "valid_image", "image_path": "images/duplicate.webp",
                 "processed_dimensions": [20, 16], "source_sha256": "d", "human_label": "keep"},
                {"source_url": "b", "status": "valid_image", "image_path": "images/b.webp",
                 "processed_dimensions": [20, 16], "source_sha256": "b", "human_label": "reject"},
                {"source_url": "missing", "status": "valid_image", "image_path": "images/missing.webp",
                 "processed_dimensions": [20, 16], "source_sha256": "m"},
                {"source_url": "wrong-size", "status": "valid_image", "image_path": "images/a.webp",
                 "processed_dimensions": [21, 16], "source_sha256": "s"},
                {"source_url": "wrong-hash", "status": "valid_image", "image_path": "images/b.webp",
                 "processed_dimensions": [20, 16], "processed_sha256": "0" * 64},
                {"source_url": "offline", "status": "error"},
                {"source_url": "c-wrong-first", "status": "valid_image", "image_path": "images/c.webp",
                 "processed_dimensions": [21, 16]},
                {"source_url": "c-correct-second", "status": "valid_image", "image_path": "images/c.webp",
                 "processed_dimensions": [20, 16]},
            ]
            manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            pairs = corpus / "pairs.jsonl"
            pairs.write_text(json.dumps({"left_asset_key": "source:b",
                                         "right_asset_key": "source:other"}) + "\n", encoding="utf-8")
            output = corpus / "decisions.jsonl"
            report = run(corpus, manifest, pairs, output, corpus / "report.json", 2)
            decisions = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["action"] for row in decisions], [
                "pass_technical_pending_content", "reuse_exact_asset",
                "review_near_duplicate", "quarantine_file", "quarantine_file",
                "quarantine_file", "retry_or_review_source",
                "quarantine_file", "pass_technical_pending_content",
            ])
            self.assertEqual(decisions[4]["reason"], "dimension_mismatch")
            self.assertEqual(decisions[5]["reason"], "checksum_mismatch")
            self.assertEqual(decisions[1]["first_row"], 1)
            self.assertEqual(report["distinct_valid_processed_sha256"], 3)
            self.assertEqual(report["action_counts"]["review_near_duplicate"], 1)
            streamed = corpus / "streamed.jsonl"
            stream_report = run_streaming(
                corpus, manifest, pairs, streamed, corpus / "stream_report.json", 2, chunk_rows=2
            )
            self.assertEqual(streamed.read_bytes(), output.read_bytes())
            self.assertEqual(stream_report["action_counts"], report["action_counts"])
            self.assertEqual(stream_report["distinct_valid_processed_sha256"], 3)


if __name__ == "__main__":
    unittest.main()
