"""
Chain-back engine with self-healing fetches.

For each deed we gather exactly the folios it needs (AI decides when the
instrument is complete). If a deed comes back BLANK - empty PDF, half-loaded
scan, or a stale session (the usual cause of old volumes going blank) - we
retry, and on the last try we force a fresh login and try once more, instead
of silently recording a blank.
"""
from __future__ import annotations

from typing import Optional

import config
import extractor
from client import MDLandRecClient, PDF_DIR
from models import BookPage, ChainLink, Deed

MAX_FOLIOS_PER_DEED = 12
EXTRACT_EVERY = 2            # pull this many pages before each AI read (fewer round-trips)
FETCH_ATTEMPTS = 3            # blank-result retries per deed (last one re-logins)


def _gather_once(client: MDLandRecClient, ref: BookPage) -> Optional[Deed]:
    if not client.search_book_page(ref):
        print(f"[gather] viewer/PDF never loaded for {ref}")
        return None
    base = PDF_DIR / ref.county.replace(" ", "_") / f"{ref.book}_{ref.page}"
    base.mkdir(parents=True, exist_ok=True)
    pdf_paths: list[str] = []
    deed: Optional[Deed] = None
    i = 0
    no_more = False
    while i < MAX_FOLIOS_PER_DEED and not no_more:
        # pull a small batch of pages, THEN read the AI once (cuts API round-trips)
        pulled = 0
        while pulled < EXTRACT_EVERY and i < MAX_FOLIOS_PER_DEED:
            data = client.current_pdf_bytes()
            if not data:
                no_more = True
                break
            out = base / f"folio_{i:02d}.pdf"
            out.write_bytes(data)
            pdf_paths.append(str(out))
            print(f"[gather] folio {i}: {len(data)} bytes (pages so far: {len(pdf_paths)})")
            i += 1
            pulled += 1
            if not client.next_folio():
                no_more = True
                break
        if not pdf_paths:
            break
        deed, complete = extractor.extract(ref.county, ref.book, ref.page, pdf_paths)
        print(f"[gather] read {len(pdf_paths)} page(s), complete={complete}")
        if complete:
            break
    return deed


def _is_blank(deed: Optional[Deed]) -> bool:
    return deed is None or (not deed.grantors and not deed.grantees)


def gather_deed(client: MDLandRecClient, ref: BookPage) -> Optional[Deed]:
    """Fetch one deed, self-healing through blanks (retry, then fresh login)."""
    deed: Optional[Deed] = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        if attempt == FETCH_ATTEMPTS:
            client.refresh_login()          # last shot: stale-session cure
        deed = _gather_once(client, ref)
        if not _is_blank(deed):
            if deed is not None:
                deed.notes = (deed.notes or "") + f" [gathered {len(deed.image_paths)} folio(s)]"
            return deed
        print(f"[gather] {ref} came back blank (attempt {attempt}/{FETCH_ATTEMPTS}) - retrying")
    print(f"[gather] {ref} still blank after {FETCH_ATTEMPTS} attempts - flagging for manual review")
    return deed


def walk_from(client: MDLandRecClient, start: BookPage,
              depth: int = config.DEFAULT_CHAIN_DEPTH) -> list[ChainLink]:
    """Walk the chain using an already-logged-in client (used by batch runs)."""
    chain: list[ChainLink] = []
    seen: set[str] = set()
    current = start
    for d in range(depth + 1):
        if current is None:
            print(f"[chain] stopped at depth {d}: no prior reference to follow")
            break
        if current.key() in seen:
            print(f"[chain] loop detected at {current}; stopping")
            break
        seen.add(current.key())
        print(f"[chain] depth {d}: {current}")
        deed = gather_deed(client, current)
        if deed is None:
            print(f"[chain] depth {d}: could not retrieve {current}")
            break
        chain.append(ChainLink(depth=d, deed=deed))
        current = deed.prior_reference
    return chain


def build_chain(start: BookPage, depth: int = config.DEFAULT_CHAIN_DEPTH) -> list[ChainLink]:
    with MDLandRecClient() as client:
        client.login()
        return walk_from(client, start, depth)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("book")
    ap.add_argument("page")
    ap.add_argument("--county", default=config.COUNTY)
    ap.add_argument("--depth", type=int, default=config.DEFAULT_CHAIN_DEPTH)
    a = ap.parse_args()
    start = BookPage(county=config.resolve_county(a.county), book=a.book, page=a.page)
    for link in build_chain(start, a.depth):
        d = link.deed
        print(f"\n#{link.depth}  {d.reference}")
        print(f"    grantor(s): {', '.join(d.grantor_names()) or '-'}")
        print(f"    grantee(s): {', '.join(d.grantee_names()) or '-'}")
        print(f"    prior:      {d.prior_reference or '-'}")
