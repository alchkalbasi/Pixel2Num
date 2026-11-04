"""Bridge between the bot and extract_numbers (SPEC R-JOB, R-OCR). No HTTP: a plain function call.

Turns a job's downloaded images into the finished .xlsx report."""
import logging
import tempfile
from pathlib import Path

LOG = logging.getLogger(__name__)
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/tiff": ".tif", "image/bmp": ".bmp"}


class Bridge:
    def __init__(self, config):
        self.layout = config.layout
        self.code_len = config.code_len

    def load(self):
        """Load PaddleOCR once, at startup. Slow; the first call may take a while."""
        from . import extract_numbers
        extract_numbers.load_ocr()

    def convert(self, job, images):
        """images: [(original_name, bytes, mime, start, count)] in batch order. Returns .xlsx bytes."""
        from . import extract_numbers as x
        # Temp names are positions, never the user's file names (no collisions, no path tricks).
        with tempfile.TemporaryDirectory(prefix="pixel2num-job-") as tmp:
            entries = []
            for position, (name, content, mime, start, count) in enumerate(images, 1):
                path = Path(tmp) / f"{position:03d}{EXTENSIONS.get(mime, '.img')}"
                path.write_bytes(content)
                entries.append(x.Entry(name, path, start, count))
            records = x.build_records(entries, False, self.layout, self.code_len)
            x.mark_repeated(records)
            return x.workbook_bytes(records)
