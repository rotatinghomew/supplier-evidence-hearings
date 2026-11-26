"""核对业务方交换材料的格式和来源引用。"""
from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(name):
    return json.loads((ROOT / "inputs" / (name + ".json")).read_text(encoding="utf-8"))


class Exchanges(unittest.TestCase):
    def test_遮盖件和有效期跨过送达(self):
        disclosure = source("disclosure")
        records = {(row["id"], row["rev"]): row for row in disclosure["materials"]}
        full = (ROOT / "evidence" / records[("G1", 1)]["file"]).read_text()
        redacted = (ROOT / "evidence" / records[("G1", 2)]["file"]).read_text()
        self.assertIn("780", full)
        self.assertNotIn("780", redacted)
        receipts = source("delivery")
        self.assertTrue(all(tuple(doc) in records for row in receipts for doc in row["docs"]))
        old_end = source("appeals")["agents"][0]["valid_until"]
        self.assertLess(receipts[0]["at"], old_end)
        self.assertGreater(receipts[1]["at"], old_end)
        self.assertEqual(receipts[0]["bundle"], receipts[1]["bundle"])
