"""End-to-end browser test against a running instance.

python3 tools/e2e_test.py http://localhost:8123 <admin-key> <out_dir>
"""
import re
import sqlite3
import sys

from playwright.sync_api import sync_playwright

BASE, KEY, OUT = sys.argv[1:4]
DB = sys.argv[4] if len(sys.argv) > 4 else None
results = []


def check(label, ok, extra=""):
    results.append(ok)
    print(("PASS" if ok else "FAIL"), "-", label, extra)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1500, "height": 950}, accept_downloads=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    # --- admin sign-in with a wrong key, then the right one
    page.goto(BASE + "/")
    page.fill("#keyInput", "wrong")
    page.click("#loginForm button")
    page.wait_for_selector("#loginErr:not(:empty)")
    check("wrong admin key rejected", "not correct" in page.inner_text("#loginErr"))
    page.fill("#keyInput", KEY)
    page.click("#loginForm button")
    page.wait_for_selector("#list .item")
    check("admin sees 76 presenters", page.locator("#list .item").count() == 76, page.inner_text("#count"))

    # --- search
    page.fill("#q", "saurav")
    check("search narrows list", page.locator("#list .item").count() == 1)
    page.fill("#q", "robotics")
    check("search matches category", page.locator("#list .item").count() == 1)
    page.fill("#q", "62")
    check("search matches slide number", page.locator("#list .item").count() >= 1)
    page.fill("#q", "saurav")
    page.click("#list .item")
    page.wait_for_selector("#editorView:not([hidden])")
    page.wait_for_function("document.getElementById('slideImg').src.startsWith('data:image')")
    check("opens Saurav Kumar on slide 62", "Saurav Kumar" in page.inner_text("#pName") and "62" in page.inner_text("#pSlide"))
    page.screenshot(path=f"{OUT}/01_admin_open.png")

    # --- edit + save
    page.fill("#rows .row:nth-child(1) textarea:nth-of-type(1)", "Schools & colleges")
    page.fill("#rows .row:nth-child(1) textarea:nth-of-type(2)", "Intro to principals who want a robotics lab, budget ₹5 lakh+")
    page.click("#btnAdd")
    page.fill("#rows .row:nth-child(2) textarea:nth-of-type(1)", "Corporate CSR heads")
    page.fill("#rows .row:nth-child(2) textarea:nth-of-type(2)", "Sponsor an AI lab for a government school")
    check("status shows unsaved", "Unsaved" in page.inner_text("#status"))
    page.wait_for_timeout(1800)
    check("meter says it fits", "Fits" in page.inner_text("#meter"), page.inner_text("#meter"))
    page.click("#btnSave")
    page.wait_for_function("document.getElementById('status').textContent.includes('All changes saved')")
    check("status shows saved", True)
    page.screenshot(path=f"{OUT}/02_admin_saved.png")

    # --- persistence after reload (new page, same session)
    page.reload()
    page.wait_for_selector("#list .item")
    check("list shows saved dot after reload", page.locator("#list .dot.on").count() == 1)
    page.fill("#q", "saurav")
    page.click("#list .item")
    page.wait_for_selector("#rows textarea")
    check("saved text persisted", page.input_value("#rows .row:nth-child(1) textarea:nth-of-type(2)").startswith("Intro to principals"))

    # --- too-long text blocks saving
    page.fill("#rows .row:nth-child(1) textarea:nth-of-type(2)", ("very long sentence " * 40).strip()[:600])
    page.click("#btnAdd"); page.click("#btnAdd"); page.click("#btnAdd")
    for i in range(3, 6):
        page.fill(f"#rows .row:nth-child({i}) textarea:nth-of-type(2)", ("lengthy text " * 45).strip()[:600])
        page.fill(f"#rows .row:nth-child({i}) textarea:nth-of-type(1)", ("left " * 100).strip()[:500])
    page.wait_for_timeout(2500)
    meter = page.inner_text("#meter")
    check("too-long text disables Save", page.is_disabled("#btnSave") and "Too long" in meter, meter[:90])
    page.screenshot(path=f"{OUT}/03_too_long.png")

    # --- presenter link view: only own page
    page.goto(BASE + "/")  # discard unsaved edits
    page.on("dialog", lambda d: d.accept())
    ctx2 = b.new_context(viewport={"width": 1300, "height": 900})
    token = None
    if DB:
        token = sqlite3.connect(DB).execute("select token from presenters where page=62").fetchone()[0]
        token63 = sqlite3.connect(DB).execute("select token from presenters where page=63").fetchone()[0]
    pg2 = ctx2.new_page()
    pg2.goto(f"{BASE}/p/{token}")
    pg2.wait_for_selector("#editorView:not([hidden])")
    pg2.wait_for_function("document.getElementById('slideImg').src.startsWith('data:image')")
    check("presenter link shows no presenter list", not pg2.is_visible("#list"))
    check("presenter sees own saved data", pg2.input_value("#rows .row:nth-child(1) textarea:nth-of-type(1)") == "Schools & colleges")
    check("presenter has no admin controls", not pg2.is_visible("#btnDeck"))
    pg2.screenshot(path=f"{OUT}/04_presenter_view.png")
    with pg2.expect_download() as dl:
        pg2.click("#btnSlidePdf")
    dl.value.save_as(f"{OUT}/presenter_slide62.pdf")
    check("presenter can download own slide PDF", True, dl.value.suggested_filename)
    r = pg2.request.get(f"{BASE}/api/presenter/63", headers={"X-Token": token})
    check("presenter blocked from another slide (403)", r.status == 403)
    r = pg2.request.get(f"{BASE}/api/export/deck.pdf", headers={"X-Token": token})
    check("presenter blocked from full deck (403)", r.status == 403)
    pg2.goto(f"{BASE}/p/not-a-real-token")
    pg2.wait_for_selector("text=Link not valid")
    check("bad link shows friendly message", True)

    # --- two-tab overwrite protection
    pg3 = ctx2.new_page(); pg3.goto(f"{BASE}/p/{token}")
    pg3.wait_for_selector("#rows textarea")
    pg2.goto(f"{BASE}/p/{token}"); pg2.wait_for_selector("#rows textarea")
    pg2.fill("#rows .row:nth-child(1) textarea:nth-of-type(1)", "Edited in tab A")
    pg2.wait_for_timeout(1500); pg2.click("#btnSave")
    pg2.wait_for_function("document.getElementById('status').textContent.includes('All changes saved')")
    pg3.fill("#rows .row:nth-child(1) textarea:nth-of-type(1)", "Edited in stale tab B")
    pg3.wait_for_timeout(1500); pg3.click("#btnSave")
    pg3.wait_for_selector("#banner.on")
    check("stale tab cannot overwrite (conflict banner)", "somewhere else" in pg3.inner_text("#bannerText"))

    # --- admin full deck download
    page.goto(BASE + "/")
    page.wait_for_selector("#list .item")
    with page.expect_download(timeout=120000) as dl2:
        page.click("#btnDeck")
    dl2.value.save_as(f"{OUT}/updated_deck.pdf")
    check("admin can download updated deck", True, dl2.value.suggested_filename)

    # --- mobile layout sanity
    mctx = b.new_context(viewport={"width": 390, "height": 844})
    mp = mctx.new_page(); mp.goto(f"{BASE}/p/{token}")
    mp.wait_for_selector("#editorView:not([hidden])")
    mp.wait_for_function("document.getElementById('slideImg').src.startsWith('data:image')")
    overflow = mp.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
    check("mobile: no horizontal scroll", not overflow)
    mp.screenshot(path=f"{OUT}/05_mobile.png", full_page=True)

    check("no JavaScript errors", not errors, "; ".join(errors)[:200])
    b.close()

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
