"""
MDLandRec Playwright client. Login + session + book/page search + deed-PDF
download, calibrated against landrec.msa.maryland.gov (2026).

Self-healing: waits for the real PDF to load before grabbing it, rejects
empty/placeholder downloads, and can force a fresh login when a session goes
stale (which is what makes old scanned volumes start returning blanks).
"""
from __future__ import annotations

import argparse
import re
import time
from urllib.parse import urljoin
from typing import Optional

from playwright.sync_api import Page, sync_playwright

import config
from models import BookPage

SELECTORS = {
    "login_user": "#body_tbUsername",
    "login_pass": "#body_tbPassword",
    "login_submit": "#body_btnSubmit",
    "usercode_input": "#body_tbUsercode",
    "login_username_marker": "#body_tbUsername",
    "county_select": "#body_ddlbarcounties",
    "clerk_input": "#body_tbClerk",
    "book_input": "#body_tbjtnvBook",
    "page_input": "#body_tbjtnvPage",
    "jump_button": "#body_btnjtnvGo",
    "pdf_iframe": "#body_iframePDF",
    "next_page_button": "#body_btnNext",
    "prev_page_button": "#body_btnPrevious",
}

SEARCH_URL = config.BASE_URL + "Pages/Search.aspx"
PDF_DIR = config.DATA_DIR / "pdfs"
MIN_PDF_BYTES = 3000          # anything smaller is a placeholder / not a real page


def _parse_liber(book: str):
    """Split 'G.T.C. No. 1062' into ('G.T.C.', '1062'); '7230' -> ('', '7230')."""
    b = str(book).strip()
    m = re.search(r"(\d+)\s*$", b)
    if not m:
        return "", b
    number = m.group(1)
    prefix = b[:m.start()].strip()
    prefix = re.sub(r"(?i)\bno\.?\s*$", "", prefix).strip()
    return prefix, number


class MDLandRecClient:
    def __init__(self, headful: Optional[bool] = None):
        self.headful = config.HEADFUL if headful is None else headful
        self._pw = None
        self.browser = None
        self.context = None
        self.page: Optional[Page] = None
        self.on_access_code = None   # GUI hook: called instead of console input()

    def __enter__(self) -> "MDLandRecClient":
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=not self.headful)
        storage = str(config.SESSION_FILE) if config.SESSION_FILE.exists() else None
        self.context = self.browser.new_context(storage_state=storage)
        self.page = self.context.new_page()
        return self

    def __exit__(self, *exc):
        try:
            if self.context:
                self.context.storage_state(path=str(config.SESSION_FILE))
        finally:
            if self.browser:
                self.browser.close()
            if self._pw:
                self._pw.stop()

    def _pause(self):
        # Politeness delay, capped at 1s so runs stay fast but still well-behaved.
        time.sleep(min(config.REQUEST_DELAY_SECONDS, 1.0))

    # -- auth ---------------------------------------------------------------
    def is_logged_in(self) -> bool:
        self.page.goto(SEARCH_URL, wait_until="domcontentloaded")
        return self.page.locator(SELECTORS["login_username_marker"]).count() == 0

    def login(self) -> None:
        if self.is_logged_in():
            print("[login] existing saved session still valid - no login needed")
            return
        if not (config.USERNAME and config.PASSWORD):
            raise RuntimeError("Set MDLANDREC_USERNAME / MDLANDREC_PASSWORD in .env")
        print("[login] filling email + password...")
        self.page.goto(config.BASE_URL, wait_until="domcontentloaded")
        self.page.fill(SELECTORS["login_user"], config.USERNAME)
        self.page.fill(SELECTORS["login_pass"], config.PASSWORD)
        self.page.click(SELECTORS["login_submit"])
        self.page.wait_for_load_state("networkidle")
        self._pause()
        if self.page.locator(SELECTORS["usercode_input"]).count() > 0:
            print("\n  ACCESS CODE REQUIRED - enter the emailed code in the browser, "
                  "submit it there.")
            hook = self.on_access_code or ACCESS_CODE_HOOK
            if callable(hook):
                hook()                          # GUI: blocks until user clicks Continue
            else:
                input("Press Enter AFTER you have logged in in the browser... ")
        if self.is_logged_in():
            self.context.storage_state(path=str(config.SESSION_FILE))
            print("[login] success - session saved.")
        else:
            raise RuntimeError("Still not logged in.")

    def refresh_login(self) -> None:
        """Force a clean session: drop cookies + saved state, then log in again.
        This is the automatic cure for a stale session (old volumes going blank)."""
        print("[login] session looks stale - refreshing login automatically...")
        try:
            self.context.clear_cookies()
        except Exception:
            pass
        try:
            if config.SESSION_FILE.exists():
                config.SESSION_FILE.unlink()
        except Exception:
            pass
        self.login()

    # -- county / search ----------------------------------------------------
    def _select_county(self, county: str) -> None:
        sel = self.page.locator(SELECTORS["county_select"])
        if not sel.count():
            print("[county] dropdown not found")
            return
        cl = county.strip().lower()
        options = sel.locator("option")
        chosen = None
        for i in range(options.count()):
            opt = options.nth(i)
            txt = (opt.inner_text() or "").strip().lower()
            if txt in (cl, cl + " county") or txt.replace(" county", "") == cl:
                chosen = opt.get_attribute("value")
                break
        sel.select_option(value=chosen) if chosen is not None else sel.select_option(label=county)
        self.page.wait_for_load_state("networkidle")
        self._pause()

    def _current_pdf_url(self) -> Optional[str]:
        iframe = self.page.locator(SELECTORS["pdf_iframe"])
        if not iframe.count():
            return None
        src = iframe.first.get_attribute("src")
        if not src:
            return None
        return urljoin(self.page.url, src).replace(" ", "%20")

    def _pdf_ready(self) -> bool:
        url = (self._current_pdf_url() or "").lower()
        return ".pdf" in url and "loading" not in url

    def _wait_for_pdf(self, timeout: float = 25.0) -> bool:
        """Poll until the viewer's iframe holds a real PDF (not a loading spinner)."""
        end = time.time() + timeout
        while time.time() < end:
            if self._pdf_ready():
                return True
            time.sleep(0.5)
        return False

    def search_book_page(self, ref: BookPage) -> bool:
        self.page.goto(SEARCH_URL, wait_until="domcontentloaded")
        self._select_county(ref.county)
        clerk, booknum = _parse_liber(ref.book)
        self.page.fill(SELECTORS["clerk_input"], clerk)          # always clear/set clerk
        if clerk:
            print(f"[jump] old-style liber: clerk={clerk!r} book={booknum!r}")
        self.page.fill(SELECTORS["book_input"], booknum)
        self.page.fill(SELECTORS["page_input"], ref.page)
        self.page.click(SELECTORS["jump_button"])
        self.page.wait_for_load_state("networkidle")
        self._pause()
        return self._wait_for_pdf()          # only True once a real PDF has loaded

    def current_pdf_bytes(self, retries: int = 4) -> Optional[bytes]:
        """Download the current folio PDF, retrying if it's empty/too small."""
        for attempt in range(retries):
            if not self._wait_for_pdf(timeout=10.0):
                time.sleep(1.5)
                continue
            url = self._current_pdf_url()
            if url:
                resp = self.context.request.get(url)
                if resp.ok:
                    body = resp.body()
                    if len(body) >= MIN_PDF_BYTES:
                        return body
                    print(f"[pdf] got only {len(body)} bytes (attempt {attempt+1}) - retrying")
            time.sleep(1.5)
        return None

    def next_folio(self) -> bool:
        nxt = self.page.locator(SELECTORS["next_page_button"])
        if not nxt.count() or not nxt.first.is_enabled():
            return False
        nxt.first.click()
        self.page.wait_for_load_state("networkidle")
        self._pause()
        return True


def _dump_controls(page, label: str) -> None:
    controls = page.eval_on_selector_all(
        "input, select, button, a, img, iframe",
        """els => els.map(e => ({
            tag: e.tagName, id: e.id, name: e.getAttribute('name'),
            type: e.getAttribute('type'),
            src: (e.getAttribute('src') || '').slice(0, 80),
            text: (e.innerText || e.value || '').trim().slice(0, 40)
        }))"""
    )
    print(f"\n--- {len(controls)} controls on {label} ({page.url}) ---")
    for c in controls:
        print(c)


def _inspect(which: str) -> None:
    with MDLandRecClient(headful=True) as c:
        if which == "search":
            try:
                c.login()
            except Exception as e:
                print(f"(login skipped/failed: {e})")
        c.page.goto(SEARCH_URL if which == "search" else config.BASE_URL,
                    wait_until="domcontentloaded")
        time.sleep(2)
        _dump_controls(c.page, which)
        input("\nPress Enter to close the browser... ")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", choices=["login", "search"])
    ap.add_argument("--test-login", action="store_true")
    args = ap.parse_args()
    if args.inspect:
        _inspect(args.inspect)
    elif args.test_login:
        with MDLandRecClient() as c:
            c.login()
            print("logged_in:", c.is_logged_in())
