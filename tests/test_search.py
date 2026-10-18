import sqlite3
import unittest

from app.server import fold, search


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
            CREATE TABLE images (id INTEGER PRIMARY KEY, image_path TEXT, entity_names TEXT,
                                 caption TEXT, region TEXT, page_title TEXT, search_text TEXT);
            CREATE VIRTUAL TABLE images_fts USING fts5(names, caption, title, region, description);
            CREATE TABLE entity_region (entity_name TEXT, folded_name TEXT, region TEXT, folded_region TEXT);
        """)
        rows = [
            (1, "images/a.webp", "Bãi biển Nha Trang", "Bãi biển", "Khánh Hòa"),
            (2, "images/b.webp", "Lễ hội Huế", "Biểu diễn lễ hội", "Huế"),
            (3, "images/c.webp", "Ẩm thực Huế", "Món ăn", "Huế"),
        ]
        for row in rows:
            image_id, path, name, caption, region = row
            value = fold(" ".join((name, caption, region)))
            self.con.execute("INSERT INTO images VALUES (?,?,?,?,?,?,?)",
                             (image_id, path, name, caption, region, name, value))
            self.con.execute("INSERT INTO images_fts(rowid,names,caption,title,region,description) VALUES (?,?,?,?,?,?)",
                             (image_id, fold(name), fold(caption), fold(name), fold(region), ""))
        self.con.execute("INSERT INTO entity_region VALUES (?,?,?,?)",
                         ("Đại Nội Huế", fold("Đại Nội Huế"), "Huế", fold("Huế")))

    def tearDown(self):
        self.con.close()

    def test_single_intent(self):
        found = search(self.con, "bãi biển Nha Trang")
        self.assertEqual(found["type"], "single_intent")
        self.assertEqual(found["results"][0]["image_url"], "/images/a.webp")

    def test_multi_intent_has_two_groups(self):
        found = search(self.con, "lễ hội và ẩm thực", limit=2)
        self.assertEqual(found["type"], "multi_intent")
        self.assertEqual(len(found["intents"]), 2)
        self.assertIn("/images/b.webp", {row["image_url"] for row in found["results"]})
        self.assertIn("/images/c.webp", {row["image_url"] for row in found["results"]})

    def test_multi_hop_uses_grounded_region(self):
        found = search(self.con, "lễ hội ở cùng tỉnh với Đại Nội Huế")
        self.assertEqual(found["type"], "multi_hop")
        self.assertEqual(found["resolved_region"], "Huế")
        self.assertEqual([row["image_url"] for row in found["results"]], ["/images/b.webp"])

    def test_unknown_relation_does_not_invent_answer(self):
        found = search(self.con, "lễ hội ở cùng tỉnh với nơi không tồn tại")
        self.assertEqual(found["results"], [])
        self.assertIn("reason", found)

    def test_ambiguous_region_does_not_pick_one(self):
        self.con.execute("INSERT INTO entity_region VALUES (?,?,?,?)",
                         ("Đại Nội Huế", fold("Đại Nội Huế"), "Quảng Nam", fold("Quảng Nam")))
        found = search(self.con, "lễ hội ở cùng tỉnh với Đại Nội Huế")
        self.assertEqual(found["results"], [])
        self.assertEqual(len(found["candidate_regions"]), 2)


if __name__ == "__main__":
    unittest.main()
