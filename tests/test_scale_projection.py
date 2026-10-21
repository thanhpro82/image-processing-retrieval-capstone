import unittest

from scripts.project_processing_scale import scenario


class ScaleProjectionTests(unittest.TestCase):
    def test_optional_shadow_gpu_hours_are_separate_from_cpu_and_download(self):
        cpu = {"throughput_assets_per_second": 100, "mean_bytes_per_asset": 50000}
        pilot = {"mean_source_bytes_per_valid": 100000, "valid_images_per_sec": 10}
        safety = {"images_per_second": 20}
        result = scenario(1_000_000, cpu, pilot, None, None, safety, None)
        self.assertEqual(result["cpu_stage_machine_hours_at_sample_throughput"], 2.78)
        self.assertEqual(result["adult_content_shadow_gpu_hours_at_pilot_rate"], 13.89)
        self.assertIsNone(result["adult_content_shadow_usd_if_gpu_rate_supplied"])
        self.assertEqual(result["download_hours_at_pilot_rate_without_parallel_scale"], 27.78)

    def test_no_shadow_report_does_not_invent_gpu_cost(self):
        cpu = {"throughput_assets_per_second": 100, "mean_bytes_per_asset": 50000}
        pilot = {"mean_source_bytes_per_valid": 100000, "valid_images_per_sec": 10}
        result = scenario(1_000_000, cpu, pilot, 1.0, 1.0, None, 2.0)
        self.assertIsNone(result["adult_content_shadow_gpu_hours_at_pilot_rate"])
        self.assertIsNone(result["adult_content_shadow_usd_if_gpu_rate_supplied"])


if __name__ == "__main__":
    unittest.main()
