"""HARNESS L2: the library and the CLI must read exactly what the original script read.

Needs PaddleOCR and real card photos, which are never committed (SPEC R-SEC-3):
    tests/fixtures/cards/*.jpg  named START-END, plus tests/fixtures/baseline.json
(or the same layout under $PIXEL2NUM_FIXTURES/../). Skips when anything is missing.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARDS = Path(os.environ.get("PIXEL2NUM_FIXTURES", ROOT / "tests" / "fixtures" / "cards"))
BASELINE = CARDS.parent / "baseline.json"


def missing_reason():
    if not CARDS.is_dir() or not any(CARDS.iterdir()) or not BASELINE.is_file():
        return f"no fixtures: need photos in {CARDS} and {BASELINE}"
    try:
        import cv2, paddleocr  # noqa: F401
    except ImportError as error:
        return f"OCR packages not installed ({error.name})"
    return None


@unittest.skipIf(missing_reason(), missing_reason() or "")
class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads(BASELINE.read_text())

    def test_library_matches_baseline(self):
        from pixel2num import extract_numbers as x
        files = sorted((p for p in CARDS.iterdir() if p.suffix.lower() in x.IMAGE_EXTS), key=x.natural_key)
        records = x.build_records(x.entries_from_files(files))
        x.mark_repeated(records)
        self.assertEqual(json.loads(json.dumps(records)), self.baseline)

    def test_cli_matches_baseline_and_codes_are_text(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.xlsx"
            subprocess.run([sys.executable, "-m", "pixel2num.extract_numbers", str(CARDS), str(out)],
                           cwd=ROOT, check=True, capture_output=True)
            sheet = load_workbook(out).active
            rows = [list(r) for r in sheet.iter_rows(min_row=2, values_only=True)]
            self.assertEqual(rows, self.baseline)
            self.assertTrue(all(c.number_format == "@" for c in sheet["C"][1:]))


if __name__ == "__main__":
    unittest.main()
