import json
import tempfile
import unittest
from pathlib import Path

from scripts.pilot_safety_shadow import select_distinct_sample


class SafetySampleTests(unittest.TestCase):
    def test_seeded_sample_uses_distinct_decoded_assets(self):
        rows = [
            {"status": "valid_image", "image_path": f"images/{index}.webp"}
            for index in range(20)
        ]
        rows += [{"status": "valid_image", "image_path": "images/0.webp"},
                 {"status": "error", "image_path": "images/bad.webp"}]
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "manifest.jsonl"
            manifest.write_text("".join(json.dumps(row) + "\n" for row in rows),
                                encoding="utf-8")
            selected, available = select_distinct_sample(manifest, 7, 123)
            again, _ = select_distinct_sample(manifest, 7, 123)
            self.assertEqual(available, 20)
            self.assertEqual(selected, again)
            self.assertEqual(len({row["image_path"] for row in selected}), 7)
            self.assertNotIn("images/bad.webp", [row["image_path"] for row in selected])


if __name__ == "__main__":
    unittest.main()
