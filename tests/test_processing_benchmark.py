import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.benchmark_processing import percentile, process_one


class ProcessingBenchmarkTests(unittest.TestCase):
    def test_hash_decode_and_dimensions_are_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            corpus = Path(folder)
            image_path = corpus / "test.webp"
            Image.new("RGB", (17, 13), (50, 100, 150)).save(image_path, format="WEBP")
            expected_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
            row = {
                "image_path": image_path.name,
                "processed_sha256": expected_hash,
                "processed_dimensions": [17, 13],
            }
            result = process_one(corpus, row)
            self.assertEqual(len(result["dhash"]), 16)
            self.assertGreater(result["bytes"], 0)
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                process_one(corpus, {**row, "processed_sha256": "0" * 64})
            with self.assertRaisesRegex(ValueError, "Dimension mismatch"):
                process_one(corpus, {**row, "processed_dimensions": [18, 13]})

    def test_percentile_interpolates(self):
        self.assertEqual(percentile([4, 1, 3, 2], 0.5), 2.5)
        self.assertAlmostEqual(percentile([4, 1, 3, 2], 0.95), 3.85)


if __name__ == "__main__":
    unittest.main()
