"""BNI Jetkings - SPECIFIC ASK editor.

Presenters edit a "SPECIFIC ASK" table for their own slide of the master deck
(assets/deck.pdf). Tables are drawn into the measured free space of the real
slide pages; the original pages are never altered.

Run:  python3 app.py            (dev server)
      gunicorn -w 2 -b 0.0.0.0:8000 app:app   (production-style)
"""
import base64
import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import subprocess
import tempfile
import threading
from datetime import datetime, timezone

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory
from pypdf import PdfReader, PdfWriter

import layout
from reportlab.pdfgen import canvas as rl_canvas

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BNI_DATA_DIR", os.path.join(BASE, "data"))
DECK = os.path.join(BASE, "assets", "deck.pdf")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

MAX_ROWS = 8
MAX_CELL = 600
MAX_HEADER = 40
PREVIEW_W = 1200

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
MAX_JSON_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_VIDEO_BYTES + 1024 * 1024  # real per-route limits applied in before_request
MEDIA_DIR = os.path.join(DATA_DIR, "media")
os.makedirs(MEDIA_DIR, exist_ok=True)
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
VIDEO_EXT = {"mp4", "webm", "mov", "m4v"}
VIDEO_MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mov": "video/quicktime"}

with open(os.path.join(BASE, "assets", "presenters.json"), encoding="utf-8") as fh:
    PRESENTERS = {p["page"]: p for p in json.load(fh)}

# ---------------------------------------------------------------- admin key
def _load_key(env_name, filename):
    env = os.environ.get(env_name)
    if env:
        return env
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        return open(path).read().strip()
    key = secrets.token_urlsafe(18)
    with open(path, "w") as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    return key


ADMIN_KEY = _load_key("BNI_ADMIN_KEY", "admin_key.txt")
VIEWER_KEY = _load_key("BNI_VIEWER_KEY", "viewer_key.txt")  # read-only access

# ----------------------------------------------------------------- database
DB_PATH = os.path.join(DATA_DIR, "asks.db")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def _close(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS presenters(
            page INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS asks(
            page INTEGER PRIMARY KEY, headers TEXT NOT NULL, rows TEXT NOT NULL,
            version INTEGER NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ask_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, page INTEGER NOT NULL, version INTEGER NOT NULL,
            headers TEXT NOT NULL, rows TEXT NOT NULL, saved_at TEXT NOT NULL, saved_by TEXT NOT NULL);
        """
    )
    # Migration: the "Do you have a specific ask? Yes/No" switch. Slides that already had
    # content before this column existed stay switched on, so nothing disappears.
    for table in ("asks", "ask_history"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "enabled" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"UPDATE {table} SET enabled=1 WHERE rows <> '[]'")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS media(
            page INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, kind TEXT, filename TEXT,
            stored TEXT, link TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    have = {r[0] for r in conn.execute("SELECT page FROM presenters")}
    for page in PRESENTERS:
        if page not in have:
            conn.execute("INSERT INTO presenters(page, token) VALUES(?,?)", (page, secrets.token_urlsafe(16)))
    conn.commit()
    conn.close()


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------- auth
def _eq(a, b):
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def is_admin():
    return _eq(request.headers.get("X-Admin-Key", ""), ADMIN_KEY)


def is_viewer():
    return _eq(request.headers.get("X-Viewer-Key", ""), VIEWER_KEY)


def token_page():
    tok = request.headers.get("X-Token", "")
    if not tok:
        return None
    row = db().execute("SELECT page FROM presenters WHERE token=?", (tok,)).fetchone()
    return row["page"] if row else None


def authorize(page, write=True):
    """Admins may touch any presenter page; presenters only the page their link maps to.
    Viewers (view-only key) may read any page but are refused everything that changes data
    (write=True is the default, so a new route is write-protected unless marked read-only)."""
    if page not in PRESENTERS:
        abort(404)
    if is_admin():
        return "admin"
    if token_page() == page:
        return "presenter"
    if not write and is_viewer():
        return "viewer"
    abort(403)


def require_admin():
    if not is_admin():
        abort(403)


def require_staff():
    """Admin or view-only key (read-only listings and exports)."""
    if not (is_admin() or is_viewer()):
        abort(403)


@app.before_request
def _limit_body():
    if request.method in ("POST", "PUT", "PATCH") and not (
            request.path.startswith("/api/media/") and request.path.endswith("/file")):
        request.max_content_length = MAX_JSON_BYTES


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(403)
def _e403(_e):
    return jsonify(error="forbidden", message="You don't have access to this page."), 403


@app.errorhandler(404)
def _e404(_e):
    return jsonify(error="not_found", message="Not found."), 404


@app.errorhandler(500)
def _e500(_e):
    return jsonify(error="server_error", message="The server hit a problem. See the server window for details."), 500


@app.errorhandler(413)
def _e413(_e):
    return jsonify(error="too_large", message="That request is too large."), 413


# ------------------------------------------------------------- PDF helpers
_page_cache = {}
_page_lock = threading.Lock()


def single_page_pdf(page):
    """Bytes of a one-page PDF for the given 1-based deck page (cached)."""
    with _page_lock:
        if page in _page_cache:
            return _page_cache[page]
        path = os.path.join(CACHE_DIR, f"page-{page:03d}.pdf")
        if not os.path.exists(path):
            reader = PdfReader(DECK)
            w = PdfWriter()
            w.add_page(reader.pages[page - 1])
            with open(path, "wb") as fh:
                w.write(fh)
        data = open(path, "rb").read()
        _page_cache[page] = data
        return data


def merge_overlay(page_bytes, overlay_bytes):
    base = PdfReader(io.BytesIO(page_bytes)).pages[0]
    base.merge_page(PdfReader(io.BytesIO(overlay_bytes)).pages[0])
    w = PdfWriter()
    w.add_page(base)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def render_png(pdf_bytes, width=PREVIEW_W):
    """PDF page -> PNG bytes. Uses pypdfium2 (pip-installable, works on Windows/Mac/Linux);
    falls back to poppler's pdftoppm if pypdfium2 is not installed."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium is not None:
        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            page = pdf[0]
            scale = width / float(page.get_width())
            img = page.render(scale=scale).to_pil().convert("RGB")
            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
        finally:
            pdf.close()
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.pdf")
        with open(src, "wb") as fh:
            fh.write(pdf_bytes)
        subprocess.run(
            ["pdftoppm", "-png", "-singlefile", "-scale-to-x", str(width), "-scale-to-y", "-1", src, os.path.join(td, "out")],
            check=True, capture_output=True, timeout=60,
        )
        return open(os.path.join(td, "out.png"), "rb").read()


# --------------------------------------------------------------- validation
class Invalid(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def parse_payload(page, data):
    if not isinstance(data, dict):
        raise Invalid("bad_request", "Invalid request.")
    headers = data.get("headers", list(layout.DEFAULT_HEADERS))
    rows = data.get("rows", [])
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise Invalid("bad_request", "Invalid request.")
    if not (isinstance(headers, list) and len(headers) == 2 and all(isinstance(h, str) for h in headers)):
        raise Invalid("bad_request", "Column headings are invalid.")
    if not (isinstance(rows, list) and len(rows) <= MAX_ROWS + 20):
        raise Invalid("bad_request", "Rows are invalid.")
    clean_rows = []
    for r in rows:
        if not (isinstance(r, list) and len(r) == 2 and all(isinstance(c, str) for c in r)):
            raise Invalid("bad_request", "Rows are invalid.")
        a, b = layout.clean(r[0]), layout.clean(r[1])
        if a or b:
            clean_rows.append([a, b])
    if len(clean_rows) > MAX_ROWS:
        raise Invalid("too_many_rows", f"Please use at most {MAX_ROWS} rows.")
    hdr = [layout.clean(headers[0]), layout.clean(headers[1])]
    for h in hdr:
        if len(h) > MAX_HEADER or "\n" in h:
            raise Invalid("header_too_long", f"Column headings must be one line, up to {MAX_HEADER} characters.")
    for a, b in clean_rows:
        if len(a) > MAX_CELL or len(b) > MAX_CELL:
            raise Invalid("cell_too_long", f"Each cell can hold up to {MAX_CELL} characters.")
    if not enabled:
        # "No specific ask": the table is not drawn, so there is nothing to fit.
        # (Any text typed earlier is kept so switching back to Yes restores it.)
        return hdr, clean_rows, None, False
    bad = layout.unsupported_chars("".join(hdr) + "".join(a + b for a, b in clean_rows))
    if bad:
        raise Invalid(
            "unsupported_chars",
            "These characters can't be shown on the slide: " + " ".join(bad) + ". Please replace them (Kannada/other scripts aren't supported yet).",
            chars=bad,
        )
    box = PRESENTERS[page]["free_box"]
    lay = layout.compute(box, hdr, clean_rows)
    return hdr, clean_rows, lay, True


def fit_info(lay):
    if lay is None:
        return {"fits": True, "font": None, "overflow_pt": 0, "avail_h": 0, "used_h": 0}
    return {
        "fits": lay["fits"], "font": lay["font"], "overflow_pt": round(lay["overflow"], 1),
        "avail_h": round(lay["avail_h"], 1), "used_h": round(lay["total_h"], 1),
    }


def stored(page):
    row = db().execute("SELECT * FROM asks WHERE page=?", (page,)).fetchone()
    if not row:
        return {"enabled": False, "headers": list(layout.DEFAULT_HEADERS), "rows": [], "version": 0, "updated_at": None, "updated_by": None}
    return {
        "enabled": bool(row["enabled"]), "headers": json.loads(row["headers"]), "rows": json.loads(row["rows"]),
        "version": row["version"], "updated_at": row["updated_at"], "updated_by": row["updated_by"],
    }


def presenter_public(page):
    p = PRESENTERS[page]
    return {"page": page, "name": p["name"], "category": p["category"], "company": p["company"]}


# --------------------------------------------------------------------- pages
@app.route("/")
@app.route("/p/<token>")
def index(token=None):
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/session")
def session():
    if is_admin():
        return jsonify(role="admin", slide_count=len(PdfReader(DECK).pages), presenters=len(PRESENTERS))
    if is_viewer():
        return jsonify(role="viewer", slide_count=len(PdfReader(DECK).pages), presenters=len(PRESENTERS))
    page = token_page()
    if page:
        return jsonify(role="presenter", presenter=presenter_public(page))
    abort(403)


@app.route("/api/presenters")
def presenters():
    require_staff()
    saved = {r["page"]: r for r in db().execute("SELECT page, version, updated_at, rows, enabled FROM asks")}
    out = []
    for page in sorted(PRESENTERS):
        s = saved.get(page)
        has = bool(s and s["enabled"] and json.loads(s["rows"]))
        out.append({**presenter_public(page), "saved": has, "enabled": bool(s and s["enabled"]), "updated_at": s["updated_at"] if s else None,
                    "version": s["version"] if s else 0})
    return jsonify(out)


@app.route("/api/presenter/<int:page>", methods=["GET"])
def get_presenter(page):
    role = authorize(page, write=False)
    return jsonify({**presenter_public(page), **stored(page), "role": role,
                    "limits": {"rows": MAX_ROWS, "cell": MAX_CELL, "header": MAX_HEADER},
                    "default_headers": list(layout.DEFAULT_HEADERS)})


@app.route("/api/presenter/<int:page>", methods=["PUT"])
def save_presenter(page):
    role = authorize(page)
    data = request.get_json(silent=True)
    try:
        headers, rows, lay, enabled = parse_payload(page, data)
        expected = int((data or {}).get("version", 0))
    except Invalid as e:
        return jsonify(error=e.code, message=e.message, **e.extra), 422
    except (TypeError, ValueError):
        return jsonify(error="bad_request", message="Invalid request."), 400
    if lay is not None and not lay["fits"]:
        return jsonify(error="too_long", message="This text is too long to fit on the slide. Please shorten it.",
                       **fit_info(lay)), 422

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("SELECT version FROM asks WHERE page=?", (page,)).fetchone()
        ts = now_iso()
        if cur is None:
            if expected != 0:
                raise sqlite3.IntegrityError("stale")
            new_version = 1
            conn.execute("INSERT INTO asks(page,headers,rows,version,updated_at,updated_by,enabled) VALUES(?,?,?,?,?,?,?)",
                         (page, json.dumps(headers), json.dumps(rows), new_version, ts, role, int(enabled)))
        else:
            new_version = cur["version"] + 1
            n = conn.execute(
                "UPDATE asks SET headers=?, rows=?, version=?, updated_at=?, updated_by=?, enabled=? WHERE page=? AND version=?",
                (json.dumps(headers), json.dumps(rows), new_version, ts, role, int(enabled), page, expected)).rowcount
            if n == 0:
                raise sqlite3.IntegrityError("stale")
        conn.execute("INSERT INTO ask_history(page,version,headers,rows,saved_at,saved_by,enabled) VALUES(?,?,?,?,?,?,?)",
                     (page, new_version, json.dumps(headers), json.dumps(rows), ts, role, int(enabled)))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify(error="conflict",
                       message="This page was changed somewhere else since you opened it. Reload to see the latest version.",
                       current=stored(page)), 409
    except Exception:
        conn.rollback()
        raise
    return jsonify(ok=True, version=new_version, updated_at=ts, enabled=enabled, headers=headers, rows=rows, **fit_info(lay))


@app.route("/api/history/<int:page>")
def history(page):
    authorize(page, write=False)
    rows = db().execute(
        "SELECT version, enabled, headers, rows, saved_at, saved_by FROM ask_history WHERE page=? ORDER BY id DESC LIMIT 25",
        (page,)).fetchall()
    return jsonify([{"version": r["version"], "enabled": bool(r["enabled"]), "headers": json.loads(r["headers"]), "rows": json.loads(r["rows"]),
                     "saved_at": r["saved_at"], "saved_by": r["saved_by"]} for r in rows])


@app.route("/api/preview/<int:page>", methods=["POST"])
def preview(page):
    authorize(page, write=False)  # renders only; writes nothing
    data = request.get_json(silent=True)
    try:
        _h, _r, lay, _en = parse_payload(page, data)
    except Invalid as e:
        return jsonify(error=e.code, message=e.message, **e.extra), 422
    if lay is None:  # "No specific ask": show the untouched slide
        png = render_png(single_page_pdf(page))
        return jsonify(image="data:image/png;base64," + base64.b64encode(png).decode(), **fit_info(lay))
    overlay = layout.overlay_pdf(lay)
    png = render_png(merge_overlay(single_page_pdf(page), overlay))
    return jsonify(image="data:image/png;base64," + base64.b64encode(png).decode(), **fit_info(lay))


@app.route("/api/slide/<int:page>.png")
def slide_png(page):
    authorize(page, write=False)
    path = os.path.join(CACHE_DIR, f"slide-{page:03d}.png")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(render_png(single_page_pdf(page)))
    return send_from_directory(CACHE_DIR, os.path.basename(path), max_age=0)


# -------------------------------------------------------- image / video slide
def media_row(page):
    return db().execute("SELECT * FROM media WHERE page=?", (page,)).fetchone()


def media_info(page):
    r = media_row(page)
    if not r:
        return {"enabled": False, "kind": None, "filename": None, "link": "", "url": None, "updated_at": None}
    has_file = bool(r["stored"])
    return {"enabled": bool(r["enabled"]), "kind": r["kind"] if (has_file or r["link"]) else None,
            "filename": r["filename"] if has_file else None, "link": r["link"],
            "url": f"/media/{r['key']}" if has_file else None, "updated_at": r["updated_at"]}


def media_upsert(page, **fields):
    conn = db()
    r = media_row(page)
    ts = now_iso()
    if r is None:
        conn.execute("INSERT INTO media(page,enabled,kind,filename,stored,link,key,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (page, fields.get("enabled", 0), fields.get("kind"), fields.get("filename"), fields.get("stored"),
                      fields.get("link", ""), secrets.token_urlsafe(18), ts))
    else:
        fields["updated_at"] = ts
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE media SET {cols} WHERE page=?", (*fields.values(), page))
    conn.commit()


def _remove_stored(name):
    if name:
        try:
            os.remove(os.path.join(MEDIA_DIR, name))
        except OSError:
            pass


@app.route("/api/media/<int:page>", methods=["GET"])
def get_media(page):
    authorize(page, write=False)
    return jsonify({**media_info(page), "limits": {"image_mb": MAX_IMAGE_BYTES // 2**20, "video_mb": MAX_VIDEO_BYTES // 2**20}})


@app.route("/api/media/<int:page>", methods=["PUT"])
def put_media(page):
    authorize(page)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
        return jsonify(error="bad_request", message="Invalid request."), 400
    fields = {"enabled": int(data["enabled"])}
    if "link" in data:
        link = data["link"].strip() if isinstance(data["link"], str) else None
        if link is None or len(link) > 500 or (link and not link.lower().startswith(("http://", "https://"))):
            return jsonify(error="bad_link", message="A video link must start with http:// or https://"), 422
        fields["link"] = link
        r = media_row(page)
        if link and not (r and r["stored"]):
            fields["kind"] = "video"
    media_upsert(page, **fields)
    return jsonify(ok=True, **media_info(page))


@app.route("/api/media/<int:page>/file", methods=["POST"])
def upload_media(page):
    authorize(page)
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no_file", message="Please choose an image or video file."), 422
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext in IMAGE_EXT:
        kind, limit = "image", MAX_IMAGE_BYTES
    elif ext in VIDEO_EXT:
        kind, limit = "video", MAX_VIDEO_BYTES
    else:
        return jsonify(error="bad_type", message="Unsupported file. Use an image (PNG, JPG, GIF, WEBP) or video (MP4, WEBM, MOV)."), 422
    stored = f"{page}-{secrets.token_hex(6)}.{ext}"
    path = os.path.join(MEDIA_DIR, stored)
    size = 0
    with open(path, "wb") as out:
        while True:
            chunk = f.stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                break
            out.write(chunk)
    if size > limit:
        _remove_stored(stored)
        return jsonify(error="too_large", message=f"That file is too large (limit {limit // 2**20} MB)."), 413
    if kind == "image":
        try:
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
        except Exception:
            _remove_stored(stored)
            return jsonify(error="bad_image", message="That image could not be read. Try a PNG or JPG."), 422
    old = media_row(page)
    media_upsert(page, enabled=1, kind=kind, filename=os.path.basename(f.filename)[:120], stored=stored)
    if old:
        _remove_stored(old["stored"])
    return jsonify(ok=True, **media_info(page))


@app.route("/api/media/<int:page>/file", methods=["DELETE"])
def delete_media_file(page):
    authorize(page)
    r = media_row(page)
    if r:
        _remove_stored(r["stored"])
        media_upsert(page, kind=None, filename=None, stored=None, link="")
    return jsonify(ok=True, **media_info(page))


@app.route("/media/<key>")
def serve_media(key):
    r = db().execute("SELECT stored FROM media WHERE key=?", (key,)).fetchone()
    if not r or not r["stored"] or not r["stored"].replace("-", "").replace(".", "").isalnum():
        abort(404)
    ext = r["stored"].rsplit(".", 1)[-1]
    resp = send_from_directory(MEDIA_DIR, r["stored"], mimetype=VIDEO_MIME.get(ext), conditional=True, max_age=0)
    resp.headers["Content-Security-Policy"] = "sandbox"
    return resp


def _latin(text):
    return text.encode("latin-1", "replace").decode("latin-1")


def media_slide_pdf(page, base_url, include_empty=False):
    """Extra blank slide (deck size) holding the presenter's image/video, or None."""
    r = media_row(page)
    if not r or not r["enabled"]:
        return None
    has_img = r["stored"] and r["kind"] == "image"
    has_vid = (r["stored"] and r["kind"] == "video") or r["link"]
    if not (has_img or has_vid) and not include_empty:
        return None
    W, H = layout.PAGE_W, layout.PAGE_H
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(W, H))
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    if has_img:
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        with Image.open(os.path.join(MEDIA_DIR, r["stored"])) as im:
            im.seek(0)
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[3])
        bg.thumbnail((2600, 2600))
        m = 50
        scale = min((W - 2 * m) / bg.width, (H - 2 * m) / bg.height)
        w, h = bg.width * scale, bg.height * scale
        c.drawImage(ImageReader(bg), (W - w) / 2, (H - h) / 2, w, h)
    elif has_vid:
        url = r["link"] or f"{base_url}/media/{r['key']}"
        bw, bh = 1000, 562.5
        x, y = (W - bw) / 2, (H - bh) / 2
        c.setFillColorRGB(0.11, 0.12, 0.16)
        c.roundRect(x, y, bw, bh, 18, stroke=0, fill=1)
        c.setFillColorRGB(0.81, 0.13, 0.19)
        c.circle(W / 2, H / 2 + 20, 62, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        p = c.beginPath()
        p.moveTo(W / 2 - 18, H / 2 + 20 + 32); p.lineTo(W / 2 - 18, H / 2 + 20 - 32); p.lineTo(W / 2 + 34, H / 2 + 20); p.close()
        c.drawPath(p, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 30)
        c.drawCentredString(W / 2, y + 110, "Click to play video")
        c.setFont("Helvetica", 20)
        c.setFillColorRGB(0.8, 0.82, 0.88)
        label = _latin(r["filename"] or r["link"])
        c.drawCentredString(W / 2, y + 74, label[:80])
        c.linkURL(url, (x, y, x + bw, y + bh), relative=0)
    c.showPage()
    c.save()
    return buf.getvalue()


def overlay_for_saved(page, include_empty=False):
    s = stored(page)
    if not s["enabled"]:  # presenter has no specific ask -> slide stays untouched
        return None
    if not s["rows"] and not include_empty:
        return None
    lay = layout.compute(PRESENTERS[page]["free_box"], s["headers"], s["rows"])
    return layout.overlay_pdf(lay)


def pdf_response(data, filename):
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def safe_name(name):
    return "".join(ch if ch.isalnum() else "_" for ch in name).strip("_") or "presenter"


@app.route("/api/export/slide/<int:page>.pdf")
def export_slide(page):
    authorize(page, write=False)
    base = single_page_pdf(page)
    ov = overlay_for_saved(page)
    data = merge_overlay(base, ov) if ov else base
    extra = media_slide_pdf(page, request.host_url.rstrip("/"))
    if extra:
        w = PdfWriter()
        for part in (data, extra):
            w.add_page(PdfReader(io.BytesIO(part)).pages[0])
        out = io.BytesIO()
        w.write(out)
        data = out.getvalue()
    return pdf_response(data, f"slide-{page:03d}-{safe_name(PRESENTERS[page]['name'])}.pdf")


_deck_cache = {}
_deck_lock = threading.Lock()


def deck_fingerprint():
    c = db()
    asks = c.execute("SELECT page, version, enabled FROM asks ORDER BY page").fetchall()
    media = c.execute("SELECT page, enabled, link, stored, updated_at FROM media ORDER BY page").fetchall()
    return repr([tuple(r) for r in asks]) + repr([tuple(r) for r in media])


def build_deck(include_empty, base_url):
    """The master deck with every saved Specific Ask table and image/video slide merged in.
    Cached until any presenter's data changes, so many viewers don't each rebuild 190+ pages."""
    key = (deck_fingerprint(), include_empty, base_url)
    with _deck_lock:
        if key in _deck_cache:
            return _deck_cache[key]
    overlays = {}
    for page in PRESENTERS:
        ov = overlay_for_saved(page, include_empty)
        if ov:
            overlays[page] = ov
    reader = PdfReader(DECK)  # fresh reader: merge_page mutates page objects
    writer = PdfWriter()
    for i, pg in enumerate(reader.pages, start=1):
        if i in overlays:
            pg.merge_page(PdfReader(io.BytesIO(overlays[i])).pages[0])
        writer.add_page(pg)
        if i in PRESENTERS:  # image/video slide goes straight after the presenter's slide
            extra = media_slide_pdf(i, base_url, include_empty)
            if extra:
                writer.add_page(PdfReader(io.BytesIO(extra)).pages[0])
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    with _deck_lock:
        _deck_cache.clear()
        _deck_cache[key] = data
    return data


@app.route("/api/export/deck.pdf")
def export_deck():
    require_staff()
    data = build_deck(request.args.get("include_empty") == "1", request.host_url.rstrip("/"))
    return pdf_response(data, "BNI-Jetkings-OCT-updated.pdf")


# ---- public viewer: "I'm a viewer" opens the updated deck straight away, no key needed
@app.route("/view")
def view_deck():
    return send_from_directory(app.static_folder, "view.html")


@app.route("/deck.pdf")
def public_deck():
    data = build_deck(False, request.host_url.rstrip("/"))
    resp = Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="BNI-Jetkings-OCT-updated.pdf"'})
    resp.headers["Cache-Control"] = "no-store"
    if request.args.get("download") == "1":
        resp.headers["Content-Disposition"] = 'attachment; filename="BNI-Jetkings-OCT-updated.pdf"'
    return resp


@app.route("/api/links.csv")
def links_csv():
    require_admin()
    tokens = {r["page"]: r["token"] for r in db().execute("SELECT page, token FROM presenters")}
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["slide", "name", "category", "personal_link"])
    base = request.host_url.rstrip("/")
    for page in sorted(PRESENTERS):
        p = PRESENTERS[page]
        wr.writerow([page, p["name"], p["category"], f"{base}/p/{tokens[page]}"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="presenter-links.csv"'})


@app.route("/api/link/<int:page>")
def link_one(page):
    require_admin()
    row = db().execute("SELECT token FROM presenters WHERE page=?", (page,)).fetchone()
    if not row:
        abort(404)
    return jsonify(link=request.host_url.rstrip("/") + "/p/" + row["token"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  Admin key : {ADMIN_KEY}   (full access)\n  Viewer key: {VIEWER_KEY}   (view only - can't change anything)\n"
          f"  (stored in {DATA_DIR} as admin_key.txt / viewer_key.txt unless BNI_ADMIN_KEY / BNI_VIEWER_KEY are set)\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
