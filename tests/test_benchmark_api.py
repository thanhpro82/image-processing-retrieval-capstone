import unittest
from unittest.mock import patch

from scripts.benchmark_api import timed_request


class BenchmarkApiTests(unittest.TestCase):
    def test_timeout_remains_a_measured_error(self):
        with patch("scripts.benchmark_api.request", side_effect=TimeoutError("slow")):
            answer, elapsed_ms, error = timed_request("http://localhost:8080", "test")
        self.assertIsNone(answer)
        self.assertGreaterEqual(elapsed_ms, 0)
        self.assertEqual(error, "TimeoutError")


if __name__ == "__main__":
    unittest.main()
