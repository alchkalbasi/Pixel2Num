"""HARNESS L2: the library and the CLI must read exactly what the original
script read.

Needs PaddleOCR and real card photos, which are never committed
(SPEC R-SEC-3):
    tests/fixtures/cards/*.jpg  named START-END,
    plus tests/fixtures/baseline.json
(or the same layout under $PIXEL2NUM_FIXTURES/../).
Skips when anything is missing.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
CARDS = Path(os.environ.get(
    "PIXEL2NUM_FIXTURES", ROOT / "tests" / "fixtures" / "cards"))
BASELINE = CARDS.parent / "baseline.json"


def missing_reason():
    """Why the tests must be skipped, or None when they can run."""
    if (not CARDS.is_dir() or not any(CARDS.iterdir())
            or not BASELINE.is_file()):
        return f"no fixtures: need photos in {CARDS} and {BASELINE}"
    # These packages are only imported to check that they are installed.
    # They stay inside the function so the whole test module can still be
    # imported (and skipped) in the fast environment without OCR packages.
    try:
        import cv2  # noqa: F401
        import paddleocr  # noqa: F401
    except ImportError as error:
        return f"OCR packages not installed ({error.name})"
    return None


@unittest.skipIf(missing_reason(), missing_reason() or "")
class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(BASELINE.read_text())

    def test_library_matches_baseline(self):
        # Imported here: extract_numbers needs OpenCV, which is missing in
        # the fast environment (this test is skipped there).
        from pixel2num import extract_numbers as x
        files = sorted(
            (p for p in CARDS.iterdir() if p.suffix.lower() in x.IMAGE_EXTS),
            key=x.natural_key)
        records = x.build_records(x.entries_from_files(files))
        x.mark_repeated(records)
        self.assertEqual(json.loads(json.dumps(records)), self.baseline)

    def test_cli_matches_baseline_and_codes_are_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.xlsx"
            subprocess.run(
                [sys.executable, "-m", "pixel2num.extract_numbers",
                 str(CARDS), str(out)],
                cwd=ROOT, check=True, capture_output=True)
            sheet = load_workbook(out).active
            rows = [list(r)
                    for r in sheet.iter_rows(min_row=2, values_only=True)]
            self.assertEqual(rows, self.baseline)
            self.assertTrue(
                all(c.number_format == "@" for c in sheet["C"][1:]))

    def test_records_are_sorted_by_counter(self):
        """Images given out of order still give rows from the smallest
        counter to the biggest. OCR is faked: only the sorting is tested."""
        from pixel2num import extract_numbers as x
        empty = {"slots": {}, "extras": [], "reliable": True,
                 "code_len": 15, "layout": "right", "variant": "original"}
        entries = [x.Entry("21-22.jpg", Path("b"), 21, 2),
                   x.Entry("bad.jpg", Path("c"), None, 0),
                   x.Entry("1-3.jpg", Path("a"), 1, 3)]
        with patch.object(x, "process", return_value=empty):
            records = x.build_records(entries)
        self.assertEqual([r[1] for r in records], [1, 2, 3, 21, 22, ""])


if __name__ == "__main__":
    unittest.main()
