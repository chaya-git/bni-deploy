"""Identify presenter slides in the deck PDF and measure free white space on each.

Usage: python3 -I inspect_deck.py <deck.pdf> <thumbs_dir (40dpi t-NNN.png)> <out.json>
"""
import json
import re
import sys

import numpy as np
import pdfplumber
from PIL import Image

PDF, THUMBS, OUT = sys.argv[1:4]
PAGE_W, PAGE_H = 1440.0, 810.0
SCALE = 800.0 / PAGE_W  # 40 dpi thumbnails are 800x450


def thumb(p):
    return Image.open(f"{THUMBS}/t-{p:03d}.png").convert("RGB")


def largest_empty_rect(free, min_w, min_h):
    """free: 2D bool array (True = empty). Largest-area all-True rectangle."""
    h, w = free.shape
    heights = np.zeros(w, dtype=int)
    best = (0, 0, 0, 0, 0)  # area, x0, y0, x1, y1
    for y in range(h):
        heights = np.where(free[y], heights + 1, 0)
        stack = []
        for x in range(w + 1):
            cur = heights[x] if x < w else 0
            start = x
            while stack and stack[-1][1] >= cur:
                sx, sh = stack.pop()
                area = sh * (x - sx)
                if area > best[0] and (x - sx) >= min_w and sh >= min_h:
                    best = (area, sx, y - sh + 1, x, y + 1)
                start = sx
            stack.append((start, cur))
    return best


presenters = []
with pdfplumber.open(PDF) as pdf:
    for idx, page in enumerate(pdf.pages, start=1):
        words = page.extract_words(extra_attrs=["size"], keep_blank_chars=False)
        text = " ".join(w["text"] for w in words)
        if not (re.search(r"Category\s*:", text) and re.search(r"Company\s*:", text)):
            continue
        if "New Induction" in text:
            continue  # induction welcome slides: not individual weekly-presentation pages
        cat = next((w for w in words if w["text"].startswith("Category")), None)
        if cat is None:
            continue
        cat_x0, cat_top = cat["x0"], cat["top"]

        # presenter name: biggest text above the Category line, right of the photo column
        above = [w for w in words if w["bottom"] <= cat_top + 5 and w["size"] > 30
                 and w["top"] > 120 and w["x0"] > 300]
        name = ""
        if above:
            top_line = min(w["top"] for w in above)
            line = [w for w in above if abs(w["top"] - top_line) < 25]
            line_y = [w for w in above if w["top"] >= top_line - 2]
            name = " ".join(w["text"] for w in sorted(line_y, key=lambda w: (round(w["top"] / 30), w["x0"])))
        name = re.sub(r"\s+", " ", name).strip().title()

        nxt = next((w for w in words if w["text"].startswith("Next") and w["top"] > cat_top), None)
        cutoff = (nxt["top"] - 5) if nxt else 690
        detail = [w for w in words if w["x0"] >= cat_x0 - 15 and w["top"] >= cat_top - 5 and w["bottom"] < min(690, cutoff)]
        detail_bottom = max([w["bottom"] for w in detail] or [cat_top + 220])

        def val(label):
            m = re.search(label + r"\s*:\s*(.*?)\s*(?=(Company|Ph|Phone|Email|URL|Next|$))", text)
            return m.group(1).strip() if m else ""

        category = val("Category")
        company = val("Company")

        # free-space search in the right-hand area below the details
        im = np.asarray(thumb(idx)).astype(int)
        bgv = np.bincount((im // 8).reshape(-1, 3).dot([1024, 32, 1]).astype(int)).argmax()
        bg = np.array([(bgv // 1024) * 8 + 4, ((bgv // 32) % 32) * 8 + 4, (bgv % 32) * 8 + 4])
        diff = np.abs(im - bg).max(axis=2) > 28
        # dilate obstacles a little (safety margin ~8pt)
        pad = max(1, int(8 * SCALE))
        d = diff.copy()
        for dy in range(-pad, pad + 1):
            for dx in range(-pad, pad + 1):
                d |= np.roll(np.roll(diff, dy, axis=0), dx, axis=1)
        x_lo = int((cat_x0 - 4) * SCALE)
        x_hi = int(1405 * SCALE)
        y_lo = int((detail_bottom + 14) * SCALE)
        y_hi = int(695 * SCALE)
        region_free = ~d[y_lo:y_hi, x_lo:x_hi]
        area, rx0, ry0, rx1, ry1 = largest_empty_rect(region_free, int(250 * SCALE), int(60 * SCALE))
        if not area:
            area, rx0, ry0, rx1, ry1 = largest_empty_rect(region_free, int(250 * SCALE), int(30 * SCALE))
        if area:
            box = [round((x_lo + rx0) / SCALE, 1), round((y_lo + ry0) / SCALE, 1),
                   round((x_lo + rx1) / SCALE, 1), round((y_lo + ry1) / SCALE, 1)]
        else:
            box = None
        presenters.append({
            "page": idx, "name": name, "category": category, "company": company,
            "detail_bottom": round(detail_bottom, 1), "cat_x0": round(cat_x0, 1),
            "free_box": box,
        })

json.dump(presenters, open(OUT, "w"), indent=1)
print(len(presenters), "presenter slides")
for p in presenters:
    b = p["free_box"]
    print(p["page"], "|", p["name"], "|", (p["category"] or "")[:30], "|", b)
