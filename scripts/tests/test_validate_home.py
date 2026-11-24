import unittest
from pathlib import Path

from validate_home import validate_document


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")


class HomeValidationTests(unittest.TestCase):
    def test_homepage(self):
        validate_document((ROOT / "index.html").read_text(encoding="utf-8"))

    def test_truncated_index(self):
        with self.assertRaisesRegex(ValueError, "missing HTML doctype"):
            validate_document((FIXTURES / "truncated-index.html").read_text(encoding="utf-8"))

    def test_feed_inserted_inside_summary(self):
        with self.assertRaisesRegex(ValueError, "HTML parsing errors"):
            validate_document((FIXTURES / "corrupted-feed.html").read_text(encoding="utf-8"))
