"""Replace the master presentation deck for BNI Jetkings website.

Usage:
    python tools/replace_deck.py <path_to_new_presentation.pptx_or_.pdf> [--dry-run]
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
DECK_PATH = os.path.join(ASSETS_DIR, "deck.pdf")
PRESENTERS_PATH = os.path.join(ASSETS_DIR, "presenters.json")

PAGE_W, PAGE_H = 1440.0, 810.0
SCALE = 800.0 / PAGE_W


def convert_pptx_to_pdf(pptx_path, out_pdf_path):
    print(f"[*] Converting PowerPoint '{pptx_path}' to PDF via PowerPoint COM...", flush=True)
    ps_code = """param([string]$src, [string]$dst)
$ppt = New-Object -ComObject PowerPoint.Application
try {
    $pres = $ppt.Presentations.Open($src, 1, 0, 0)
    $pres.SaveAs($dst, 32)
    $pres.Close()
    Write-Host 'SUCCESS'
} finally {
    $ppt.Quit()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($ppt) | Out-Null
}
"""
    ps_file = os.path.join(tempfile.gettempdir(), "convert_pptx_temp.ps1")
    with open(ps_file, "w", encoding="utf-8") as f:
        f.write(ps_code)

    try:
        res = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps_file, os.path.abspath(pptx_path), os.path.abspath(out_pdf_path)],
            capture_output=True,
            text=True
        )
        if "SUCCESS" not in res.stdout:
            raise RuntimeError(f"PowerPoint conversion failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        print("[+] PowerPoint conversion completed successfully.", flush=True)
    finally:
        if os.path.exists(ps_file):
            try:
                os.remove(ps_file)
            except Exception:
                pass


def largest_empty_rect(free, min_w, min_h):
    h, w = free.shape
    heights = np.zeros(w, dtype=int)
    best = (0, 0, 0, 0, 0)
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


def inspect_deck(pdf_path):
    print(f"[*] Scanning PDF for presenter slides and measuring free space...", flush=True)
    doc = pdfium.PdfDocument(pdf_path)
    total_pages = len(doc)
    print(f"[*] Total slides: {total_pages}", flush=True)

    presenters = []
    with pdfplumber.open(pdf_path) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            if idx % 20 == 0 or idx == total_pages:
                print(f"[*] Processed {idx}/{total_pages} slides...", flush=True)

            words = page.extract_words(extra_attrs=["size"], keep_blank_chars=False)
            text = " ".join(w["text"] for w in words)
            if not (re.search(r"Category\s*:", text) and re.search(r"Company\s*:", text)):
                continue
            if "New Induction" in text:
                continue

            cat = next((w for w in words if w["text"].startswith("Category")), None)
            if cat is None:
                continue
            cat_x0, cat_top = cat["x0"], cat["top"]

            above = [w for w in words if w["bottom"] <= cat_top + 5 and w["size"] > 30
                     and w["top"] > 120 and w["x0"] > 300]
            name = ""
            if above:
                top_line = min(w["top"] for w in above)
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

            # render 40dpi thumb with pdfium for free space detection
            p = doc[idx - 1]
            w, h = p.get_size()
            bmp = p.render(scale=800.0 / w)
            im = np.asarray(bmp.to_pil().convert("RGB")).astype(int)
            p.close()

            bgv = np.bincount((im // 8).reshape(-1, 3).dot([1024, 32, 1]).astype(int)).argmax()
            bg = np.array([(bgv // 1024) * 8 + 4, ((bgv // 32) % 32) * 8 + 4, (bgv % 32) * 8 + 4])
            diff = np.abs(im - bg).max(axis=2) > 28
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
                "page": idx,
                "name": name,
                "category": category,
                "company": company,
                "detail_bottom": round(detail_bottom, 1),
                "cat_x0": round(cat_x0, 1),
                "free_box": box,
            })

    doc.close()
    return presenters


def main():
    parser = argparse.ArgumentParser(description="Replace deck for BNI Jetkings SPECIFIC ASK website")
    parser.add_argument("file", help="Path to new .pptx or .pdf file (or folder containing it)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without replacing files")
    args = parser.parse_args()

    input_path = os.path.abspath(args.file)
    if os.path.isdir(input_path):
        # find pptx or pdf inside
        candidates = [os.path.join(input_path, f) for f in os.listdir(input_path) if f.lower().endswith((".pptx", ".ppt", ".pdf"))]
        if not candidates:
            print(f"Error: No PPTX or PDF found in folder: {input_path}")
            sys.exit(1)
        input_path = candidates[0]
        print(f"[*] Located presentation file: {input_path}", flush=True)

    if not os.path.exists(input_path):
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    ext = os.path.splitext(input_path)[1].lower()
    temp_pdf = None

    if ext in [".pptx", ".ppt"]:
        temp_pdf = os.path.join(tempfile.gettempdir(), f"deck_converted_{os.getpid()}.pdf")
        convert_pptx_to_pdf(input_path, temp_pdf)
        pdf_to_use = temp_pdf
    elif ext == ".pdf":
        pdf_to_use = input_path
    else:
        print(f"Error: Unsupported file format {ext}. Expected .pptx or .pdf")
        sys.exit(1)

    try:
        presenters = inspect_deck(pdf_to_use)
        print(f"\n[+] Found {len(presenters)} presenter slides.", flush=True)
        if not presenters:
            print("[!] Warning: 0 presenter slides found! Did the slide format or text change?", flush=True)
            print("[!] Aborting to prevent breaking the website.", flush=True)
            sys.exit(1)

        print(f"[+] Sample presenters detected:", flush=True)
        for p in presenters[:5]:
            print(f"    Page {p['page']:03d}: {p['name']} ({p['category'] or 'N/A'}) - Box: {p['free_box']}", flush=True)
        if len(presenters) > 5:
            print(f"    ... and {len(presenters) - 5} more.", flush=True)

        if args.dry_run:
            print("\n[+] Dry run complete. No files were modified.", flush=True)
            return

        # Backup old files
        if os.path.exists(DECK_PATH):
            shutil.copy2(DECK_PATH, DECK_PATH + ".bak")
            print(f"[+] Backed up old deck to {DECK_PATH}.bak", flush=True)
        if os.path.exists(PRESENTERS_PATH):
            shutil.copy2(PRESENTERS_PATH, PRESENTERS_PATH + ".bak")
            print(f"[+] Backed up old presenters to {PRESENTERS_PATH}.bak", flush=True)

        # Copy new files
        shutil.copy2(pdf_to_use, DECK_PATH)
        print(f"[+] Installed new {DECK_PATH}", flush=True)

        with open(PRESENTERS_PATH, "w", encoding="utf-8") as f:
            json.dump(presenters, f, indent=1)
        print(f"[+] Wrote updated {PRESENTERS_PATH}", flush=True)

        # Clear preview cache
        if os.path.exists(CACHE_DIR):
            for fname in os.listdir(CACHE_DIR):
                fpath = os.path.join(CACHE_DIR, fname)
                try:
                    if os.path.isfile(fpath) or os.path.islink(fpath):
                        os.unlink(fpath)
                    elif os.path.isdir(fpath):
                        shutil.rmtree(fpath)
                except Exception as e:
                    print(f"[-] Could not remove cache file {fpath}: {e}", flush=True)
            print(f"[+] Cleared slide cache in {CACHE_DIR}", flush=True)

        print("\n[SUCCESS] Master deck updated successfully!", flush=True)
        print("When the server starts or reloads, presenter tokens will automatically synchronize.", flush=True)
    finally:
        if temp_pdf and os.path.exists(temp_pdf):
            try:
                os.remove(temp_pdf)
            except Exception:
                pass


if __name__ == "__main__":
    main()
