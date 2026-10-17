# BNI Jetkings — SPECIFIC ASK editor

Each presenter edits a **SPECIFIC ASK** table (heading + two editable columns) for *their own slide* of the master deck.
The table is drawn into the measured empty space on the real slide; nothing else on any page is changed.

The table is **optional**: the editor first asks *"Do you have a specific ask?"*. **No** (the default) = no table is shown or added to the slide. **Yes** = the table editor appears and the table is drawn on the slide. Text typed before switching to No is kept, so switching back to Yes restores it. Slides that already had a saved ask before this change are switched to Yes automatically.

A second question, **"Do you want to add an image or video?"**, adds a new blank slide right after the presenter's slide.
Presenters upload an image (PNG/JPG/GIF/WEBP, up to 20 MB) or a video (MP4/WEBM/MOV, up to 200 MB) or paste a video link.
Images are placed on the new slide in the PDF; a video becomes a "Click to play" card that links to the video (PDFs can't play video inside them).
Uploaded files live in `data/media/` — back that folder up along with `data/asks.db`.

**View-only page:** a third key, the **viewer key**, opens the same dashboard (list of all presenters, slide previews, deck/slide PDF download)
with everything locked: no editing, saving, uploading, presenter links or link CSV. It is enforced on the server, not just hidden in the page.
Sign in at `/` with either the admin key or the viewer key; the server works out which one it is.

**Landing page:** opening `/` first asks *"Who are you?"*.
* **I'm a presenter** → sign-in, then the admin/editor page.
* **I'm a viewer** → the updated deck (PDF with every saved table and image/video slide) opens straight away at `/view`, no key needed.
  Anyone who has your site address can open it, so only share the address with people who may see the whole deck.
  The deck is rebuilt automatically whenever someone saves a change.

## Run it
    pip install -r requirements.txt        # slide previews use pypdfium2 (installed by this command, no extra software needed)
    python3 app.py                         # http://localhost:8000
    # production-style:  gunicorn -w 2 -b 0.0.0.0:8000 app:app

On first start an **admin key** and a **viewer key** are printed and saved to `data/admin_key.txt` / `data/viewer_key.txt`
(or set your own with `BNI_ADMIN_KEY=...` / `BNI_VIEWER_KEY=...`). Data lives in `data/asks.db` (SQLite) — back up that one file.
Other settings: `PORT`, `BNI_DATA_DIR`.

## Using it
* **Admin** (open `/`, enter the admin key): searchable list of all 76 presenters, slide preview, edit any slide,
  *Presenter links (CSV)* (one secret link per presenter), *Download updated deck (PDF)*.
* **Presenter**: opens their personal link `/p/<secret>` — sees and edits **only their own slide**.
  Can download their own slide as a PDF.
* Put it behind HTTPS when hosting publicly (the personal links are the credentials).

## How it protects data
* One row per slide in the database; saves are version-checked, so a stale tab or two people can never silently overwrite each other (they get a "reload latest" message).
* Every save is also kept in `ask_history`.
* Text that cannot fit the slide is **refused with a message** instead of being cut off or spilling over content.
  Text shrinks from 20pt down to 9pt to fit. Characters the slide fonts lack (e.g. Kannada) are refused clearly.

## Files
* `assets/deck.pdf` – master deck (never modified)   * `assets/presenters.json` – 76 presenter slides + measured free space
* `layout.py` – table fitting/drawing   * `app.py` – server/API   * `static/index.html` – dashboard
* `tools/inspect_deck.py` – re-measures slides if the deck changes   * `tools/e2e_test.py` – browser test
