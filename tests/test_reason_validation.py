import unittest

from scripts.prepare_reason_validation import REVIEW_FIELDS, case_id, sample_rows


class ReasonValidationTests(unittest.TestCase):
    def test_deterministic_stratification_and_blind_fields(self):
        rows = []
        for i, (label, reason) in enumerate([
            ("keep", "not_applicable_kept"),
            ("keep", "not_applicable_kept"),
            ("reject", "watermark_or_text_overlay"),
            ("reject", "wrong_entity_or_location"),
            ("reject", "unclear_missing_note"),
        ]):
            rows.append({
                "batch": "1", "entity_id": str(i), "source_url": f"url-{i}",
                "human_label": label, "normalized_reason": reason,
                "image_status": "valid_image",
            })
        chosen, strata = sample_rows(rows, 42, {
            "watermark_or_text_overlay": 1,
            "wrong_entity_or_location": 1,
            "other_reject": 1,
            "keep": 1,
        })
        again, _ = sample_rows(rows, 42, {
            "watermark_or_text_overlay": 1,
            "wrong_entity_or_location": 1,
            "other_reject": 1,
            "keep": 1,
        })
        self.assertEqual([case_id(row) for row in chosen],
                         [case_id(row) for row in again])
        self.assertEqual(len(chosen), 4)
        self.assertEqual(strata["keep"]["inclusion_probability"], 0.5)
        self.assertNotIn("human_label", REVIEW_FIELDS)
        self.assertNotIn("original_note", REVIEW_FIELDS)
        self.assertNotIn("source_url", REVIEW_FIELDS)


if __name__ == "__main__":
    unittest.main()
