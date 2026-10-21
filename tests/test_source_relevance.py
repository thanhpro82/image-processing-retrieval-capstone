import unittest

import numpy as np

from scripts.evaluate_source_relevance import title_coverage, top_fraction_flags


class SourceRelevanceTests(unittest.TestCase):
    def test_accented_entity_and_title_match(self):
        self.assertEqual(title_coverage("Bãi biển Nha Trang", "Du lịch biển Nha Trang"), 1.0)
        self.assertEqual(title_coverage("Bãi biển Nha Trang", "Lễ hội ở Hà Nội"), 0.0)

    def test_review_quota_is_bounded_with_tied_scores(self):
        scores = np.zeros(21)
        rows = [{"source_url": f"https://example.org/{i}"} for i in range(21)]
        chosen = top_fraction_flags(scores, rows)
        self.assertEqual(int(chosen.sum()), 3)
        self.assertTrue(np.array_equal(chosen, top_fraction_flags(scores, rows)))


if __name__ == "__main__":
    unittest.main()
