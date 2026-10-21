import unittest

from scripts.build_submission_report import render, validate


class SubmissionReportTests(unittest.TestCase):
    def inputs(self):
        audit = {"unique_processed_sha256": 80001, "invalid_count": 0,
                 "manifest_rows": 80003, "valid_decodable_image_rows": 80002,
                 "unique_existing_paths": 80001}
        gate = {"rows": 80003, "distinct_valid_processed_sha256": 80001,
                "manifest_rows_per_second": 100.0, "action_counts": {
                    "pass_technical_pending_content": 80000,
                    "review_near_duplicate": 1, "reuse_exact_asset": 1,
                    "retry_or_review_source": 1}}
        cpu = {"sampled_assets": 10000, "throughput_assets_per_second": 80.0,
               "workers": 4}
        safety = {"policy": "shadow_only_no_automatic_rejection", "images": 100,
                  "images_per_second": 20.0, "device": "cpu", "batch_size": 8,
                  "flag_counts_by_mixed_human_label": {"0.5": {"keep": 1}},
                  "human_mixed_label_counts": {"keep": 80}}
        api = {"indexed_images": 80001, "by_type": {
            name: {"requests": 10, "p50_ms": 10.0, "p95_ms": 20.0,
                   "max_ms": 30.0, "over_3000ms": 0}
            for name in ("single_intent", "multi_intent", "multi_hop")},
            "load_probe": {"requests": 100, "workers": 5,
                           "throughput_requests_per_second": 50.0,
                           "p95_ms": 200.0, "over_3000ms": 1, "errors": 1}}
        scale = {"adult_content_shadow_pilot_images_per_second": 20.0,
                 "cpu_sample_throughput_assets_per_second": 80.0,
                 "scenarios": [{"valid_assets": 1000000,
                                "cpu_stage_machine_hours_at_sample_throughput": 3.47,
                                "adult_content_shadow_gpu_hours_at_pilot_rate": 13.89,
                                "processed_storage_bytes_using_cpu_sample_mean": 50000000000}]}
        return audit, gate, cpu, safety, api, scale

    def test_rejects_inconsistent_corpus_counts(self):
        values = self.inputs()
        values[4]["indexed_images"] = 79999
        with self.assertRaisesRegex(ValueError, "Indexed image count"):
            validate(*values)

    def test_keeps_shadow_and_load_error_limits_visible(self):
        body = render(*self.inputs(), "abc1234")
        self.assertIn("không bật auto-reject", body)
        self.assertIn("1 request ≥3 giây, 1 lỗi", body)
        self.assertIn("chưa có tổng chi phí", body)
        self.assertNotIn("precision/recall NSFW =", body)

    def test_larger_shadow_sample_must_match_projection(self):
        values = self.inputs()
        corpus = {**values[3], "images": 10000, "images_per_second": 19.0,
                  "flag_counts_by_mixed_human_label": {"0.5": {"unknown": 3}},
                  "score_quantiles_by_mixed_human_label": {
                      "unknown": {"p50": 0.05, "p95": 0.1, "p99": 0.2}}}
        with self.assertRaisesRegex(ValueError, "different safety run"):
            render(*values, "abc1234", corpus)
        values[5]["adult_content_shadow_pilot_images_per_second"] = 19.0
        values[5]["adult_content_shadow_pilot_images"] = 10000
        body = render(*values, "abc1234", corpus)
        self.assertIn("10,000 ảnh", body)
        self.assertIn("3 ảnh được model gắn cờ", body)

    def test_streaming_measurement_requires_matching_decisions(self):
        values = self.inputs()
        streaming = {
            "execution_mode": "disk_backed_streaming", "rows": 80003,
            "distinct_valid_processed_sha256": 80001,
            "action_counts": values[1]["action_counts"].copy(),
            "manifest_rows_per_second": 90.0, "chunk_rows": 1000,
        }
        body = render(*values, "abc1234", streaming=streaming)
        self.assertIn("90.00 dòng/s", body)
        streaming["action_counts"]["reuse_exact_asset"] = 2
        with self.assertRaisesRegex(ValueError, "Streaming gate differs"):
            render(*values, "abc1234", streaming=streaming)


if __name__ == "__main__":
    unittest.main()
