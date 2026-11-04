"""File-name ranges: START-END.jpg gives the counters of the first and last card (SPEC R-RANGE)."""
import re
from pathlib import Path

NAME_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")  # START-END (file name without extension)


def parse_range(filename, max_cards):
    """Return (start, count) for a name like '81-100.jpg', or raise ValueError with a short reason."""
    match = NAME_RE.match(Path(filename).stem)
    if not match:
        raise ValueError("the name is not START-END")
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        raise ValueError("END is smaller than START")
    if end - start + 1 > max_cards:
        raise ValueError(f"more than {max_cards} cards in one image")
    return start, end - start + 1
