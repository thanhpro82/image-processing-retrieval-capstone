import csv
import tempfile
import unittest
from pathlib import Path

from scripts.build_reason_validation_ui import build


class ReasonValidationUiTests(unittest.TestCase):
    def test_blinded_html_escapes_image_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "reviewer_1.csv"
            output = Path(folder) / "reviewer_1.html"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=(
                    "case_id", "batch", "entity_name", "entity_description", "image_file",
                    "human_label", "original_note", "source_url"))
                writer.writeheader()
                writer.writerow({
                    "case_id": "abc", "batch": "1", "entity_name": "</script><script>alert(1)</script>",
                    "entity_description": "Mô tả", "image_file": str(Path(folder) / "a.webp"),
                    "human_label": "reject", "original_note": "PRIVATE NOTE",
                    "source_url": "PRIVATE URL",
                })
            self.assertEqual(build(source, output), 1)
            html = output.read_text(encoding="utf-8")
            self.assertNotIn("</script><script>alert(1)</script>", html)
            self.assertIn("\\u003c/script>", html)
            self.assertNotIn("PRIVATE NOTE", html)
            self.assertNotIn("PRIVATE URL", html)


if __name__ == "__main__":
    unittest.main()
