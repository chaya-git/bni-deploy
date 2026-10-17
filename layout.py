"""Table layout + overlay rendering for the SPECIFIC ASK table.

All coordinates are in PDF points. Slide boxes use a top-left origin
(like pdfplumber); they are flipped to PDF's bottom-left origin only when drawing.
"""
import io
import os

from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = 1440.0, 810.0

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
_FONTS = {
    ("main", False): ("LS", "LiberationSans-Regular.ttf"),
    ("main", True): ("LS-B", "LiberationSans-Bold.ttf"),
    ("fall", False): ("DJ", "DejaVuSans.ttf"),
    ("fall", True): ("DJ-B", "DejaVuSans-Bold.ttf"),
}
for (_k, _b), (_name, _file) in _FONTS.items():
    pdfmetrics.registerFont(TTFont(_name, os.path.join(_FONT_DIR, _file)))

_CMAP = {
    "main": pdfmetrics.getFont("LS").face.charToGlyph,
    "fall": pdfmetrics.getFont("DJ").face.charToGlyph,
}

# Deck palette (taken from the existing slide tables)
RED = HexColor("#CF2030")
HEADER_FILL = HexColor("#B4BDE0")
BODY_FILL = HexColor("#CDD3EA")
BORDER = HexColor("#1E1E1E")
TEXT = HexColor("#000000")

TITLE = "SPECIFIC ASK"
PAD_X = 7.0
PAD_Y = 4.5
LEAD = 1.22
MAX_FONT = 20.0
MIN_FONT = 9.0
MAX_TABLE_W = 900.0
COL1_RATIO = 0.38

DEFAULT_HEADERS = ("Ask", "Details")


def font_for(ch, bold):
    """Return the registered font name able to draw ch, or None if neither can."""
    cp = ord(ch)
    if cp in _CMAP["main"]:
        return _FONTS[("main", bold)][0]
    if cp in _CMAP["fall"]:
        return _FONTS[("fall", bold)][0]
    return None


def unsupported_chars(text):
    bad = []
    for ch in text:
        if ch in "\n":
            continue
        if font_for(ch, False) is None and ch not in bad:
            bad.append(ch)
    return bad


def clean(text):
    """Normalise user text: no control chars/tabs, CRLF -> LF."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "".join(ch for ch in text if ch == "\n" or ch >= " ").strip()


def _runs(text, bold):
    runs = []
    for ch in text:
        f = font_for(ch, bold) or _FONTS[("main", bold)][0]
        if runs and runs[-1][0] == f:
            runs[-1][1].append(ch)
        else:
            runs.append((f, [ch]))
    return [(f, "".join(c)) for f, c in runs]


def text_width(text, bold, size):
    return sum(pdfmetrics.stringWidth(s, f, size) for f, s in _runs(text, bold))


def wrap(text, bold, size, max_w):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            cand = f"{cur} {word}" if cur else word
            if text_width(cand, bold, size) <= max_w:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while text_width(word, bold, size) > max_w and len(word) > 1:
                n = 1
                while n < len(word) and text_width(word[: n + 1], bold, size) <= max_w:
                    n += 1
                lines.append(word[:n])
                word = word[n:]
            cur = word
        lines.append(cur)
    return lines


def _measure(font, body, headers, c1w, c2w):
    tf = min(font + 4, 24.0)
    title_h = tf * 1.5
    h1 = wrap(headers[0], True, font, c1w - 2 * PAD_X)
    h2 = wrap(headers[1], True, font, c2w - 2 * PAD_X)
    head_h = max(len(h1), len(h2)) * font * LEAD + 2 * PAD_Y
    rows = []
    total = title_h + head_h
    for a, b in body:
        la = wrap(a, False, font, c1w - 2 * PAD_X)
        lb = wrap(b, False, font, c2w - 2 * PAD_X)
        h = max(len(la), len(lb), 1) * font * LEAD + 2 * PAD_Y
        rows.append((la, lb, h))
        total += h
    return {
        "font": font, "title_font": tf, "title_h": title_h,
        "head": (h1, h2, head_h), "rows": rows, "total_h": total,
    }


def compute(box, headers, rows):
    """Fit the table into box=[x0,y0,x1,y1] (top-left origin).

    Returns a dict describing the layout, including `fits` (bool) and
    `overflow` (points of height missing at the smallest font).
    """
    bx0, by0, bx1, by1 = box
    ix0, iy0, ix1, iy1 = bx0 + 6, by0 + 6, bx1 - 6, by1 - 4
    width = min(ix1 - ix0, MAX_TABLE_W)
    avail_h = iy1 - iy0
    c1w = width * COL1_RATIO
    c2w = width - c1w

    headers = (clean(headers[0]) or DEFAULT_HEADERS[0], clean(headers[1]) or DEFAULT_HEADERS[1])
    body = [(clean(a), clean(b)) for a, b in rows if clean(a) or clean(b)]
    placeholder = not body
    if placeholder:
        body = [("", "")]

    font = MAX_FONT
    result = None
    while font >= MIN_FONT - 1e-9:
        result = _measure(font, body, headers, c1w, c2w)
        if result["total_h"] <= avail_h:
            break
        font -= 0.5
    fits = result["total_h"] <= avail_h
    result.update({
        "fits": fits,
        "overflow": max(0.0, result["total_h"] - avail_h),
        "avail_h": avail_h, "x0": ix0, "y0": iy0, "width": width,
        "c1w": c1w, "c2w": c2w, "headers": headers, "placeholder": placeholder,
        "n_rows": 0 if placeholder else len(body),
    })
    return result


def _draw_text(c, x, y, text, bold, size):
    for f, s in _runs(text, bold):
        c.setFont(f, size)
        c.drawString(x, y, s)
        x += pdfmetrics.stringWidth(s, f, size)


def draw_table(c, lay):
    """Draw one computed table onto canvas c (PDF coordinates: origin bottom-left)."""
    f = lay["font"]
    x0, top = lay["x0"], PAGE_H - lay["y0"]
    w, c1w = lay["width"], lay["c1w"]

    def rect(x, ytop, rw, rh, fill):
        c.setFillColor(fill)
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.8)
        c.rect(x, ytop - rh, rw, rh, stroke=1, fill=1)

    # title bar
    th = lay["title_h"]
    rect(x0, top, w, th, RED)
    c.setFillColor(white)
    tfont = lay["title_font"]
    tw = text_width(lay["_title"], True, tfont)
    _draw_text(c, x0 + (w - tw) / 2, top - th / 2 - tfont * 0.35, lay["_title"], True, tfont)
    y = top - th

    # header row
    h1, h2, hh = lay["head"]
    rect(x0, y, c1w, hh, HEADER_FILL)
    rect(x0 + c1w, y, w - c1w, hh, HEADER_FILL)
    c.setFillColor(TEXT)
    for col_x, lines in ((x0, h1), (x0 + c1w, h2)):
        ty = y - PAD_Y - f * 0.95
        for ln in lines:
            _draw_text(c, col_x + PAD_X, ty, ln, True, f)
            ty -= f * LEAD
    y -= hh

    # body rows
    for la, lb, rh in lay["rows"]:
        rect(x0, y, c1w, rh, BODY_FILL)
        rect(x0 + c1w, y, w - c1w, rh, BODY_FILL)
        c.setFillColor(TEXT)
        for col_x, lines in ((x0, la), (x0 + c1w, lb)):
            ty = y - PAD_Y - f * 0.95
            for ln in lines:
                _draw_text(c, col_x + PAD_X, ty, ln, False, f)
                ty -= f * LEAD
        y -= rh


def overlay_pdf(lay):
    """One-page transparent PDF (slide-sized) containing just the table."""
    lay = dict(lay, _title=TITLE)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    draw_table(c, lay)
    c.showPage()
    c.save()
    return buf.getvalue()
