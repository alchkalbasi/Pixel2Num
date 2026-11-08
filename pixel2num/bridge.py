"""Bridge between the bot and extract_numbers (SPEC R-JOB, R-OCR).

No HTTP: a plain function call. Turns a job's downloaded images into the
finished .xlsx report.

Note: ``extract_numbers`` is imported inside the methods on purpose. It
needs OpenCV (and, later, PaddleOCR), so importing it at the top of this
file would make the bot (and the fast L1 tests) depend on the OCR packages
(SPEC R-LIB-1).
"""
import logging
import tempfile
from pathlib import Path

LOG = logging.getLogger(__name__)

# Mattermost MIME type -> file extension used for the job's temp files.
EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/tiff": ".tif",
    "image/bmp": ".bmp",
}


class Bridge:
    def __init__(self, config):
        self.layout = config.layout
        self.code_len = config.code_len

    def load(self):
        """Load PaddleOCR once, at startup. Slow; the first call may take
        a while."""
        # Lazy import: keeps OCR packages out of the bot's import path.
        from . import extract_numbers
        extract_numbers.load_ocr()

    def convert(self, job, images):
        """Turn the job's images into .xlsx bytes.

        images: [(original_name, bytes, mime, start, count)] in batch order.
        """
        # Lazy import: see the module docstring.
        from . import extract_numbers as x
        # Temp names are positions, never the user's file names
        # (no collisions, no path tricks).
        with tempfile.TemporaryDirectory(prefix="pixel2num-job-") as tmp:
            entries = []
            for position, (name, content, mime, start, count) in enumerate(
                    images, 1):
                extension = EXTENSIONS.get(mime, ".img")
                path = Path(tmp) / f"{position:03d}{extension}"
                path.write_bytes(content)
                entries.append(x.Entry(name, path, start, count))
            records = x.build_records(
                entries, False, self.layout, self.code_len)
            x.mark_repeated(records)
            return x.workbook_bytes(records)
