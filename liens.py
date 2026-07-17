"""
Open-lien search via the MDLandRec grantor/grantee NAME index.

Given an owner name, it lists that person's recorded MORTGAGES / DEEDS OF TRUST
and their RELEASES, then flags mortgages with no matching release as "apparently
OPEN". A release usually cites the released instrument's book-page in its Remarks
(e.g. "4318-345"), which is how we match them up.

Because a name search returns everyone with that name, this yields CANDIDATE
open liens to verify - not a certified payoff search.

Test it standalone (does not touch the web app):
    python liens.py FARO TIMOTHY
    python liens.py COAKLEY
"""
from __future__ import annotations

import re
import sys

import config
from client import MDLandRecClient

SEARCH_URL = config.BASE_URL + "Pages/Search.aspx"

SEL = {
    "clerk": "#body_tbClerk",
    "jump_book": "#body_tbjtnvBook",
    "jump_page": "#body_tbjtnvPage",
    "last_op": "#body_ddlLastName",
    "last": "#body_tbLastName",
    "first_op": "#body_ddlFirstName",
    "first": "#body_tbFirstName",
    "party_all": "#body_rdlParty_0",
    "party_grantor": "#body_rdlParty_1",
    "party_grantee": "#body_rdlParty_2",
    "search": "#body_btnSubmit",
}

MORTGAGE_TYPES = ("mortgage", "deed of trust")
RELEASE_HINTS = ("release", "satisfaction", "reconvey")


def _book_page(text: str):
    """'Book 2239, pp. 163-164' -> ('2239', '163')."""
    m = re.search(r"book\s+([A-Za-z0-9.\-]+)\s*,?\s*(?:pp?\.?|pages?|page)?\s*(\d+)",
                  text or "", re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _rows_from_results(page, debug: bool = False) -> list:
    rows = []
    for tr in page.query_selector_all("tr"):
        cells = [(td.inner_text() or "").strip() for td in tr.query_selector_all("td")]
        if not cells:
            continue
        low = " | ".join(cells).lower()
        if ("grantor" not in low and "grantee" not in low) or "book" not in low:
            continue
        if debug and len(rows) < 4:
            print("  RAWCELLS:", cells)
        rec = {"date": "", "party": "", "name": "", "instrument": "",
               "book": "", "page": "", "remarks": cells[-1] if cells else ""}
        for c in cells:
            if re.match(r"\d{4}[-/.]\d\d[-/.]\d\d", c):
                rec["date"] = c
            lc = c.lower()
            if lc.startswith("grantor") or lc.startswith("grantee"):
                rec["party"] = "grantor" if lc.startswith("grantor") else "grantee"
                rec["name"] = re.sub(r"(?is)^grant(or|ee)\s*:\s*", "", c).split("\n")[0].strip()
            if "book" in lc and not rec["book"]:
                b, p = _book_page(c)
                if b:
                    rec["book"], rec["page"] = b, p
        for c in cells:
            u = c.split("\n")[0].strip()
            lc = u.lower()
            if (not u or re.match(r"\d{4}[-/.]", u) or lc.startswith("grantor")
                    or lc.startswith("grantee") or "book" in lc):
                continue
            if u.isupper() or any(k in lc for k in ("deed", "mortgage", "release",
                                                    "assignment", "trust", "financing",
                                                    "satisfaction", "lien")):
                rec["instrument"] = u
                break
        if rec["book"]:
            rows.append(rec)
    return rows


def search_instruments(client: MDLandRecClient, county: str, last: str,
                       first: str = "", party: str = "all", debug: bool = False) -> list:
    page = client.page
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    try:
        client._select_county(county)
    except Exception:
        pass
    for s in (SEL["clerk"], SEL["jump_book"], SEL["jump_page"]):
        try:
            page.fill(s, "")
        except Exception:
            pass
    try:
        page.select_option(SEL["last_op"], label="Is")
    except Exception:
        pass
    page.fill(SEL["last"], last.upper())
    if first:
        try:
            page.select_option(SEL["first_op"], label="Begins With")
        except Exception:
            pass
        page.fill(SEL["first"], first.upper())
    party_sel = {"grantor": SEL["party_grantor"],
                 "grantee": SEL["party_grantee"]}.get(party, SEL["party_all"])
    try:
        page.check(party_sel)
    except Exception:
        pass
    page.click(SEL["search"])
    page.wait_for_load_state("networkidle")
    return _rows_from_results(page, debug=debug)


def find_open_mortgages(rows: list) -> dict:
    mortgages = [r for r in rows
                 if any(t in (r["instrument"] or "").lower() for t in MORTGAGE_TYPES)]
    releases = [r for r in rows
                if any(h in (r["instrument"] or "").lower() for h in RELEASE_HINTS)]
    released = set()
    for rel in releases:
        blob = f"{rel.get('remarks','')} {rel.get('name','')}"
        for m in re.finditer(r"(\d+)\s*[-/]\s*(\d+)", blob):
            released.add((m.group(1), m.group(2)))
    open_m = [m for m in mortgages if (m["book"], m["page"]) not in released]
    return {"mortgages": mortgages, "releases": releases, "open": open_m}


if __name__ == "__main__":
    last = sys.argv[1] if len(sys.argv) > 1 else ""
    first = sys.argv[2] if len(sys.argv) > 2 else ""
    if not last:
        print("usage: python liens.py LASTNAME [FIRSTNAME]")
        sys.exit(1)
    county = config.COUNTY
    with MDLandRecClient() as c:
        c.login()
        rows = search_instruments(c, county, last, first, party="all", debug=True)
    print(f"\n=== {len(rows)} instruments for '{first} {last}' in {county} ===")
    for r in rows:
        print(f"  {r['date']:10}  {r['party']:8}  {(r['instrument'] or '?'):24}  "
              f"Book {r['book']}/{r['page']}  rem={r['remarks']}")
    res = find_open_mortgages(rows)
    print(f"\n  mortgages/DoT found: {len(res['mortgages'])}   releases: {len(res['releases'])}")
    print(f"  APPARENTLY OPEN (no matching release): {len(res['open'])}")
    for m in res["open"]:
        print(f"    OPEN  {m['date']}  {m['instrument']}  Book {m['book']}/{m['page']}")
