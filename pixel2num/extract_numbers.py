"""
Extract the recharge code of every top-up card from photos
and save everything to an Excel file.

IMAGE NAMES:  START-END.jpg      example: 81-100.jpg
  - The cards are read from the TOP of the photo to the BOTTOM.
  - The first card gets the counter START, the next START+1, ... up to END.
    (81-100.jpg -> 20 cards, counters 81, 82, ... 100, which matches the
     numbers you wrote on the cards by hand.)
  - So the program needs no Persian-number reading at all.

CARD TYPES (found automatically for every image, nothing to set):
  right   teal cards,  code in a box on the right side      (5-5-5 digits)
  left    blue cards,  code on the left of the card          (4-4-4-2 digits)
  middle  grey cards,  code in the middle of the card        (3-4-4-4 digits)
  Different card types can be mixed in one folder.  New types: add a line to LAYOUTS.

CODE LENGTH:
  The length is NOT fixed to 15.  For every image the program finds the length
  that most of its codes have (15, 14, 16 ... ) and a code is "succeeded" only if
  it has exactly that length, so a code with a missing digit is marked failed.

BACKGROUND:
  Nothing depends on the card colour.  If an image is not read completely, the program
  automatically tries it again with more contrast and with inverted colours
  (for dark cards) and keeps the best result.

Usage:
    pip install paddleocr paddlepaddle opencv-python openpyxl
    python extract_numbers.py images_folder results.xlsx
    python extract_numbers.py images_folder results.xlsx --debug        # also saves *_debug.jpg
    python extract_numbers.py images_folder results.xlsx --layout=left  # force one card type
    python extract_numbers.py images_folder results.xlsx --len=15       # force the code length

Excel columns:  Image | Counter | Charge Code | Status
  - Image        = image file name
  - Counter      = START..END taken from the file name
  - Charge Code  = the digits, no spaces, stored as TEXT so leading zeros are kept
  - Status       = succeeded  -> a full code was read
                   failed     -> the code could not be read (cell is empty, or holds
                                 the partial digits that were seen)
                   repeated   -> the same code appears more than once in ALL images
                                 (every copy is marked, so you can check both photos)
  - A card that could not be read stays in its own row (status failed), so the
    order and the counters of the other cards never shift.
  - Cards that overlap with UNEVEN spacing (grey cards) are numbered one after the other.
    If the number of codes found is different from the number in the file name, the
    counters cannot be trusted, so every row of that image is marked failed
    (the codes stay in the sheet so you can check them by hand).
"""
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import cv2
import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from paddleocr import PaddleOCR

# ----------------------------- CONFIG ---------------------------------
CODE_LEN = None  # None = find the code length of every image by itself. Or force e.g. 15
RIGHT_SIDE_FROM = 0.50  # teal cards: only look at text right of 50% of the image width
LAYOUT = "auto"  # "auto" = try every layout below and keep the best one, or one name
LAYOUTS = {  # where the code sits: (from, to) as a fraction of the image width
    "right": (RIGHT_SIDE_FROM, 1.00),
    "left": (0.27, 0.60),
    "middle": (0.36, 0.72),
}
COLUMN_TOL = 0.10  # codes of one image start at the same x (+- 10% of the width)
UPSCALE = 2.0  # small text reads better when enlarged
MIN_GROUP_DIGITS = 4  # ignore text pieces with fewer digits (like "333")
MIN_SHORT_DIGITS = 2  # 2-3 digit pieces are only used when they touch a code line (like "088")
MIN_PARTIAL_DIGITS = 8  # a line with fewer digits than this is noise, not a card
LINE_GAP = 1.5  # two pieces further apart than 1.5 x text height are different texts
SUCCESS, FAILED, REPEATED = "succeeded", "failed", "repeated"
NAME_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")  # START-END
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
# ----------------------------------------------------------------------

try:  # PaddleOCR 3.x
    ocr = PaddleOCR(
        lang="en",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
    )
except (TypeError, ValueError):  # PaddleOCR 2.x
    ocr = PaddleOCR(lang="en", use_angle_cls=False)


def run_ocr(img):
    """Return [(text, score, (x1, y1, x2, y2)), ...] for PaddleOCR 2.x and 3.x."""
    items = []
    try:
        for page in ocr.predict(img):
            for t, s, p in zip(page["rec_texts"], page["rec_scores"], page["rec_polys"]):
                xs, ys = [pt[0] for pt in p], [pt[1] for pt in p]
                items.append((t, float(s), (min(xs), min(ys), max(xs), max(ys))))
        return items
    except (AttributeError, TypeError, KeyError):
        pass
    for page in ocr.ocr(img) or []:
        for box, (t, s) in page or []:
            xs, ys = [pt[0] for pt in box], [pt[1] for pt in box]
            items.append((t, float(s), (min(xs), min(ys), max(xs), max(ys))))
    return items


def digits_only(text):
    # common OCR slips inside number boxes
    table = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8"})
    return re.sub(r"\D", "", text.translate(table))


def pick_code(tokens, code_len):
    """If a line has too many digits, find the pieces that add up to exactly code_len."""
    for n in range(len(tokens), 0, -1):
        for combo in combinations(tokens, n):
            if sum(len(t) for t in combo) == code_len:
                return "".join(combo)
    return "".join(tokens)


def find_rows(items, width, x_from=RIGHT_SIDE_FROM, x_to=1.0):
    """Join the digit pieces that sit on the same line into one candidate code.
    Only text between x_from and x_to (fraction of the width) is used."""
    cands, shorts = [], []
    for text, score, (x1, y1, x2, y2) in items:
        if not x_from * width <= (x1 + x2) / 2 <= x_to * width:
            continue
        d = digits_only(text)
        letters = len(re.findall(r"[A-Za-z]", text))
        if letters > 2:
            continue
        groups = len([w for w in text.split() if digits_only(w)])  # "9838 5376" = 2 groups
        piece = {"d": d, "x": x1, "cy": (y1 + y2) / 2, "h": y2 - y1, "g": groups,
                 "box": (x1, y1, x2, y2)}
        if len(d) >= MIN_GROUP_DIGITS:
            cands.append(piece)
        elif len(d) >= MIN_SHORT_DIGITS:
            shorts.append(piece)
    if not cands:
        return []

    cands.sort(key=lambda c: c["cy"])
    tol = float(np.median([c["h"] for c in cands])) * 0.6
    lines, cur = [], [cands[0]]
    for c in cands[1:]:
        if abs(c["cy"] - np.mean([k["cy"] for k in cur])) <= tol:
            cur.append(c)
        else:
            lines.append(cur)
            cur = [c]
    lines.append(cur)

    # a short piece ("088", "71") that touches the end of a code line belongs to it
    for s in sorted(shorts, key=lambda c: c["x"]):
        for line in lines:
            lh = float(np.median([c["h"] for c in line]))
            lx1 = min(c["box"][0] for c in line)
            lx2 = max(c["box"][2] for c in line)
            gap = max(s["box"][0] - lx2, lx1 - s["box"][2])
            if abs(s["cy"] - np.mean([c["cy"] for c in line])) <= 0.5 * lh and gap <= 1.5 * lh:
                line.append(s)
                break

    segments = []  # the groups of ONE code are close together; a far piece is another text
    for line in lines:
        line.sort(key=lambda c: c["x"])
        lh = float(np.median([c["h"] for c in line]))
        seg = [line[0]]
        for c in line[1:]:
            if c["box"][0] - seg[-1]["box"][2] > LINE_GAP * lh:
                segments.append(seg)
                seg = []
            seg.append(c)
        segments.append(seg)

    rows = []
    for line in segments:
        tokens = [c["d"] for c in line]
        x1 = min(c["box"][0] for c in line)
        y1 = min(c["box"][1] for c in line)
        x2 = max(c["box"][2] for c in line)
        y2 = max(c["box"][3] for c in line)
        rows.append({"digits": "".join(tokens), "tokens": tokens,
                     "groups": sum(c["g"] for c in line),  # a code is printed in groups
                     "y": (y1 + y2) / 2, "box": (x1, y1, x2, y2)})
    return rows


def pick_column(rows, width, force_len=None):
    """Several columns of digits can be in the window (code, serial number ...).
    The code column is the one with the most lines (every card has a code); a code is
    printed in groups of digits, a barcode serial number is one block, so groups count first.
    Returns (rows of that column, code length)."""
    long_rows = [r for r in rows if len(r["digits"]) >= MIN_PARTIAL_DIGITS]
    if not long_rows:
        return [], force_len or 0
    tol = COLUMN_TOL * width

    clusters = []
    for r in sorted(long_rows, key=lambda r: r["box"][0]):
        if clusters and r["box"][0] - clusters[-1][0]["box"][0] <= tol:
            clusters[-1].append(r)
        else:
            clusters.append([r])

    def common_len(rs):  # (most common length, how many lines have it)
        c = Counter(len(r["digits"]) for r in rs)
        length, n = max(c.items(), key=lambda kv: (kv[1], kv[0]))
        return length, n

    best = max(clusters, key=lambda cl: (sum(r["groups"] >= 2 for r in cl), len(cl),
                                         common_len(cl)[1]))
    code_len = force_len or common_len(best)[0]
    same = [r for r in best if len(r["digits"]) == code_len] or best
    anchor = float(np.median([r["box"][0] for r in same]))
    return [r for r in rows if abs(r["box"][0] - anchor) <= tol], code_len


def spacing_is_regular(diffs, step):
    """True when the cards are spaced evenly (a missed card = a gap of 2, 3 ... steps).
    False for overlapped cards that show strips of different height."""
    if not diffs or not step:
        return True
    bad = 0
    for d in diffs:
        q = d / step
        if not (0.6 <= q <= 1.4 or (q >= 1.7 and abs(q - round(q)) <= 0.3)):
            bad += 1
    return bad <= 0.2 * len(diffs)


def assign_slots(rows, max_slots, code_len):
    """Give every code a position 1..max_slots (top = 1) using its vertical place.
    A card that was not read leaves a gap (it is NOT skipped), so numbering never shifts.
    Returns ({position: row}, [rows that did not fit anywhere], positions_are_reliable)."""
    extras = [r for r in rows if len(r["digits"]) < MIN_PARTIAL_DIGITS]  # noise
    rows = sorted((r for r in rows if len(r["digits"]) >= MIN_PARTIAL_DIGITS),
                  key=lambda r: r["y"])
    if not rows:
        return {}, extras, True

    diffs = [b["y"] - a["y"] for a, b in zip(rows, rows[1:])]
    step = float(np.median(diffs)) if diffs else 0.0  # normal distance between cards

    if not spacing_is_regular(diffs, step):
        # uneven strips: positions cannot show a missed card -> number them in order,
        # and only trust it when the count is exactly the number of cards in the file name
        slots = {i: r for i, r in enumerate(rows, 1)}
        reliable = len(rows) == max_slots
    else:
        reliable = True
        slots, n, prev = {1: rows[0]}, 1, rows[0]
        for r in rows[1:]:
            d = r["y"] - prev["y"]
            if step and d < 0.45 * step:  # same spot twice -> extra
                if len(prev["digits"]) != code_len and len(r["digits"]) == code_len:
                    extras.append(prev)
                    slots[n] = prev = r
                else:
                    extras.append(r)
                continue
            jump = 1 if (not step or d <= 1.3 * step) else max(2, round(d / step))
            n += jump  # jump > 1 means a card was missed
            slots[n] = prev = r

    for k in [k for k in slots if k > max_slots]:  # more cards than the file name says
        extras.append(slots.pop(k))
    return slots, extras, reliable


def read_layout(items, width, layout, count, force_len):
    """Read the codes of one card layout. Returns None when nothing was found."""
    x_from, x_to = LAYOUTS[layout]
    rows, code_len = pick_column(find_rows(items, width, x_from, x_to), width, force_len)
    if not rows:
        return None
    for r in rows:  # too many digits on a line -> keep the pieces that fit the length
        if len(r["digits"]) > code_len:
            r["digits"] = pick_code(r["tokens"], code_len)
    slots, extras, reliable = assign_slots(rows, count, code_len)
    full = [r for r in slots.values() if len(r["digits"]) == code_len]
    return {"slots": slots, "extras": extras, "reliable": reliable,
            "code_len": code_len, "layout": layout, "good": len(full),
            "grouped": sum(r["groups"] >= 2 for r in full)}


def make_variants(big):
    """The same photo in 3 versions, so a card colour that confuses the OCR can still be read."""
    yield "original", big
    gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
    eq = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    yield "contrast", cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)
    yield "inverted", cv2.cvtColor(255 - eq, cv2.COLOR_GRAY2BGR)


def parse_name(path):
    """'81-100.jpg' -> (81, 20)  = (first counter, number of cards).  None if bad name."""
    m = NAME_RE.match(path.stem)
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if end < start:
        return None
    return start, end - start + 1


def process(path, start, count, debug=False):
    img = cv2.imread(str(path))
    if img is None:
        return None
    big = cv2.resize(img, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
    layouts = list(LAYOUTS) if LAYOUT == "auto" else [LAYOUT]

    best, best_score = None, None
    for variant, pic in make_variants(big):
        items = run_ocr(pic)
        for layout in layouts:
            res = read_layout(items, big.shape[1], layout, count, CODE_LEN)
            if res is None:
                continue
            ok = res["reliable"]
            # codes printed in groups first, then full-length codes, then the longer code
            # (a window that cuts off the end of the codes gives shorter ones); tie = earlier
            score = (min(res["grouped"], count) if ok else 0, min(res["good"], count) if ok else 0,
                     res["good"], res["code_len"])
            if best is None or score > best_score:
                best, best_score, res["variant"] = res, score, variant
        if best and best["good"] >= count and best["reliable"]:
            break  # everything was read - no need to try the other versions
    if best is None:
        best = {"slots": {}, "extras": [], "reliable": True, "code_len": CODE_LEN or 0,
                "layout": "-", "variant": "-", "good": 0}

    if debug:
        for k, r in best["slots"].items():
            x1, y1, x2, y2 = [int(v) for v in r["box"]]
            ok = len(r["digits"]) == best["code_len"]
            cv2.rectangle(big, (x1, y1), (x2, y2), (0, 200, 0) if ok else (0, 0, 255), 3)
            cv2.putText(big, str(start + k - 1), (max(x1 - 130, 0), y2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 255, 255), 4)
        for r in best["extras"]:
            x1, y1, x2, y2 = [int(v) for v in r["box"]]
            cv2.rectangle(big, (x1, y1), (x2, y2), (255, 0, 0), 3)
        cv2.imwrite(str(path.with_name(path.stem + "_debug.jpg")), big)
    return best


def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def build_records(files, debug=False):
    """Return [[image, counter, code, status], ...] in image order, top card first."""
    records = []
    for p in files:
        parsed = parse_name(p)
        if parsed is None:
            records.append([p.name, "", "", FAILED])
            print(f"{p.name}: bad file name - use START-END.jpg (example 81-100.jpg)")
            continue
        start, count = parsed

        res = process(p, start, count, debug)
        if res is None:
            for k in range(count):
                records.append([p.name, start + k, "", FAILED])
            print(f"{p.name}: could not open image")
            continue
        slots, extras, code_len = res["slots"], res["extras"], res["code_len"]

        if slots and max(slots) < count and res["reliable"]:
            print(f"  warning: {p.name} - nothing found after counter {start + max(slots) - 1}; "
                  f"missing cards are assumed to be at the END of the stack")
        if not res["reliable"]:
            print(f"  warning: {p.name} - cards overlap with uneven spacing and {len(slots) + len(extras)} "
                  f"codes were found, but the file name says {count}. The counters cannot be "
                  f"trusted, so every row of this image is marked {FAILED} (check it with --debug)")
        ok = 0
        for k in range(1, count + 1):
            r = slots.get(k)
            digits = r["digits"] if r else ""
            good = bool(digits) and len(digits) == code_len and res["reliable"]
            ok += good
            records.append([p.name, start + k - 1, digits, SUCCESS if good else FAILED])
        print(f"{p.name}: {ok}/{count} codes read "
              f"[layout={res['layout']}, {code_len} digits, picture={res['variant']}]"
              + (f", {len(extras)} extra detection(s) ignored" if extras else ""))
    return records


def mark_repeated(records):
    """Every succeeded row whose code appears more than once gets status 'repeated'.
    Returns {code: [(image, counter), ...]} for the repeated codes."""
    counts = Counter(r[2] for r in records if r[3] == SUCCESS)
    where = defaultdict(list)
    for r in records:
        if r[3] == SUCCESS and counts[r[2]] > 1:
            r[3] = REPEATED
            where[r[2]].append((r[0], r[1]))
    return where


def write_excel(records, out_file):
    wb = Workbook()
    ws = wb.active
    ws.title = "Codes"
    ws.append(["Image", "Counter", "Charge Code", "Status"])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for col, width in zip("ABCD", (20, 10, 22, 14)):
        ws.column_dimensions[col].width = width

    fills = {FAILED: PatternFill("solid", fgColor="FFC7CE"),  # red
             REPEATED: PatternFill("solid", fgColor="FFEB9C")}  # yellow
    for rec in records:
        ws.append(rec)
        row = ws.max_row
        code_cell = ws.cell(row=row, column=3)
        code_cell.number_format = "@"  # code = text, keeps leading zeros
        code_cell.alignment = Alignment(horizontal="left")
        ws.cell(row=row, column=2).alignment = Alignment(horizontal="left")
        fill = fills.get(rec[3])
        if fill:
            for c in range(1, 5):
                ws.cell(row=row, column=c).fill = fill
    wb.save(out_file)


def main():
    global LAYOUT, CODE_LEN
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    debug = "--debug" in sys.argv
    for a in sys.argv[1:]:
        if a.startswith("--layout="):
            LAYOUT = a.split("=", 1)[1].strip().lower()
            if LAYOUT != "auto" and LAYOUT not in LAYOUTS:
                sys.exit(f"Unknown layout '{LAYOUT}'. Use one of: auto, {', '.join(LAYOUTS)}")
        elif a.startswith("--len="):
            CODE_LEN = int(a.split("=", 1)[1])
    folder = Path(args[0]) if args else Path(".")
    out_file = Path(args[1] if len(args) > 1 else "results.xlsx").with_suffix(".xlsx")
    files = sorted((p for p in folder.iterdir()
                    if p.suffix.lower() in IMAGE_EXTS and not p.stem.endswith("_debug")),
                   key=natural_key)

    records = build_records(files, debug)
    repeated = mark_repeated(records)  # final check across ALL images

    try:
        write_excel(records, out_file)
    except PermissionError:
        print(f"\nCannot write {out_file} - close it in Excel and run again.")
        return

    if repeated:
        print("\nREPEATED codes:")
        for code, places in repeated.items():
            print(f"  {code}: " + ", ".join(f"{img} #{cnt}" for img, cnt in places))
    stats = Counter(r[3] for r in records)
    print(f"\nSaved {out_file}: {stats[SUCCESS]} succeeded, {stats[FAILED]} failed, "
          f"{stats[REPEATED]} repeated")


if __name__ == "__main__":
    main()
