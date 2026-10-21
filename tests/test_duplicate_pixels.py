import unittest

import numpy as np

from scripts.evaluate_duplicate_pixels import choose_threshold, pixel_scores


class DuplicatePixelTests(unittest.TestCase):
    def test_no_safe_threshold_falls_back_to_review(self):
        actual = np.array([True, False, True, False])
        calibration = np.ones(4, dtype=bool)
        # Both positives score below a negative; a zero-false-flag policy
        # must choose to flag none rather than auto-merge a wrong pair.
        metric, threshold = choose_threshold(
            actual, calibration, {"ncc": np.array([0.2, 0.9, 0.3, 0.8])})
        self.assertEqual(metric, "none")
        self.assertEqual(threshold, float("inf"))

    def test_identical_pixels_score_above_different_pixels(self):
        import tempfile
        from pathlib import Path

        import cv2

        with tempfile.TemporaryDirectory() as folder:
            rng = np.random.default_rng(42)
            original = rng.integers(0, 256, (64, 64), dtype=np.uint8)
            other = rng.integers(0, 256, (64, 64), dtype=np.uint8)
            a, b, c = (Path(folder) / f"{name}.png" for name in "abc")
            cv2.imwrite(str(a), original)
            cv2.imwrite(str(b), original)
            cv2.imwrite(str(c), other)
            same = pixel_scores(str(a), str(b))
            different = pixel_scores(str(a), str(c))
            self.assertGreater(same["ncc"], different["ncc"])
            self.assertGreater(same["ssim"], different["ssim"])


if __name__ == "__main__":
    unittest.main()
