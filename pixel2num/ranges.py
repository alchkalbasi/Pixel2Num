"""File-name ranges (SPEC R-RANGE).

START-END.jpg gives the counters of the first and last card.
"""
import re
from pathlib import Path

# START-END (file name without extension), spaces around the numbers allowed.
NAME_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def parse_range(filename, max_cards):
    """Return (start, count) for a name like '81-100.jpg'.

    Raises ValueError with a short reason when the name is not usable.
    """
    match = NAME_RE.match(Path(filename).stem)
    if not match:
        raise ValueError("the name is not START-END")
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        raise ValueError("END is smaller than START")
    if end - start + 1 > max_cards:
        raise ValueError(f"more than {max_cards} cards in one image")
    return start, end - start + 1
