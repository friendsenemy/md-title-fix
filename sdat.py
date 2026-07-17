"""
SDAT lookup - turn an address (or parcel/account) into the property's current
deed reference + county, using Maryland's free open-data property dataset.

Dataset: opendata.maryland.gov  ed4q-f8tm  (MDP real-property view).
NOTE: the public dataset does NOT expose current owner names, so owner-name
search is not supported here (it would require scraping SDAT's website). Address,
account-id, and parcel+county lookups all work.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

import httpx

BASE = "https://opendata.maryland.gov/resource/ed4q-f8tm.json"

# MDP 2-digit county codes -> canonical names (match the title tool's counties).
COUNTY_CODES = {
    "01": "Allegany", "02": "Anne Arundel", "03": "Baltimore City",
    "04": "Baltimore County", "05": "Calvert", "06": "Caroline", "07": "Carroll",
    "08": "Cecil", "09": "Charles", "10": "Dorchester", "11": "Frederick",
    "12": "Garrett", "13": "Harford", "14": "Howard", "15": "Kent",
    "16": "Montgomery", "17": "Prince George's", "18": "Queen Anne's",
    "19": "St. Mary's", "20": "Somerset", "21": "Talbot", "22": "Washington",
    "23": "Wicomico", "24": "Worcester",
}

# short field aliases -> dataset column names
F_ACCT = "account_id_mdp_field_acctid"
F_COUNTY = "county_name_mdp_field_cntyname"
F_CC = "record_key_county_code_sdat_field_1"
F_ADDR = "mdp_street_address_mdp_field_address"
F_CITY = "mdp_street_address_city_mdp_field_city"
F_ZIP = "mdp_street_address_zip_code_mdp_field_zipcode"
F_LIBER = "deed_reference_1_liber_mdp_field_dr1liber_sdat_field_30"
F_FOLIO = "deed_reference_1_folio_mdp_field_dr1folio_sdat_field_31"
F_LANDUSE = "land_use_code_mdp_field_lu_desclu_sdat_field_50"
F_YRBLT = "c_a_m_a_system_data_year_built_yyyy_mdp_field_yearblt_sdat_field_235"
F_SQFT = "c_a_m_a_system_data_structure_area_sq_ft_mdp_field_sqftstrc_sdat_field_241"
F_ASSESS = "current_assessment_year_total_assessment_sdat_field_172"
F_LAND = "current_cycle_data_land_value_mdp_field_names_nfmlndvl_curlndvl_and_sallndvl_sdat_field_164"
F_IMPROV = "current_cycle_data_improvements_value_mdp_field_names_nfmimpvl_curimpvl_and_salimpvl_sdat_field_165"
F_SALEDT = "sales_segment_1_transfer_date_yyyy_mm_dd_mdp_field_tradate_sdat_field_89"
F_SALEPRICE = "sales_segment_1_consideration_mdp_field_considr1_sdat_field_90"
F_RPLINK = "real_property_search_link"


def _num(v):
    try:
        return int(str(v).lstrip("0") or "0")
    except Exception:
        return v


def normalize(rec: dict) -> dict:
    """Pull the useful fields out of a raw SDAT record into a flat dict."""
    liber = (rec.get(F_LIBER) or "").strip()
    folio = (rec.get(F_FOLIO) or "").strip()
    link = rec.get(F_RPLINK)
    rp_url = link.get("url") if isinstance(link, dict) else None
    return {
        "account_id": rec.get(F_ACCT),
        "county": rec.get(F_COUNTY),                 # e.g. "Anne Arundel County"
        "county_code": rec.get(F_CC),
        "address": (rec.get(F_ADDR) or "").strip(),
        "city": (rec.get(F_CITY) or "").strip(),
        "zip": (rec.get(F_ZIP) or "").strip(),
        "book": liber.lstrip("0") or liber,          # MDLandRec wants no leading zeros
        "page": folio.lstrip("0") or folio,
        "land_use": rec.get(F_LANDUSE),
        "year_built": rec.get(F_YRBLT),
        "sqft": rec.get(F_SQFT),
        "total_assessment": _num(rec.get(F_ASSESS)),
        "land_value": _num(rec.get(F_LAND)),
        "improvement_value": _num(rec.get(F_IMPROV)),
        "last_sale_date": rec.get(F_SALEDT),
        "last_sale_price": _num(rec.get(F_SALEPRICE)),
        "sdat_url": rp_url,
    }


_SDAT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def _get(params: dict) -> list[dict]:
    """Fetch from SDAT open data. Falls back to a real browser when the plain
    request is blocked by Cloudflare's bot challenge (HTTP 403 'Just a moment')."""
    try:
        with httpx.Client(timeout=30, headers=_SDAT_HEADERS,
                          follow_redirects=True) as c:
            r = c.get(BASE, params=params)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return _get_via_browser(params)


def _get_via_browser(params: dict) -> list[dict]:
    """Cloudflare-clearing fallback: open a real Chromium, let it pass the bot
    challenge, then fetch the JSON from the same origin so the clearance applies."""
    import json as _json
    from urllib.parse import urlencode
    from playwright.sync_api import sync_playwright

    url = BASE + "?" + urlencode(params)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_context(user_agent=_SDAT_HEADERS["User-Agent"]).new_page()
        try:
            page.goto("https://opendata.maryland.gov/", wait_until="domcontentloaded",
                      timeout=45000)
            # give Cloudflare a moment to issue its clearance cookie
            for _ in range(15):
                title = (page.title() or "").lower()
                if "just a moment" not in title and "attention" not in title:
                    break
                page.wait_for_timeout(1000)
            text = page.evaluate(
                "async (u) => { const r = await fetch(u, "
                "{headers: {'Accept': 'application/json'}}); return await r.text(); }",
                url,
            )
        finally:
            browser.close()
    text = (text or "").strip()
    if not text.startswith("[") and not text.startswith("{"):
        raise RuntimeError("SDAT returned no JSON (still blocked). First 120 chars: "
                           + text[:120])
    return _json.loads(text)


def lookup_by_account(acctid: str) -> Optional[dict]:
    rows = _get({"$where": f"{F_ACCT}='{acctid}'", "$limit": 1})
    return normalize(rows[0]) if rows else None


def lookup_by_parcel(parcel_id: str, county_code: str) -> Optional[dict]:
    """Build the acctid the way SDAT does: CC + '01' + parcel[1:].zfill(11)."""
    cc = county_code.zfill(2)
    acct = cc + "01" + parcel_id[1:].zfill(11)
    return lookup_by_account(acct)


def _norm_addr(s: str) -> str:
    s = (s or "").upper()
    s = re.sub(r"[.,]", " ", s)
    subs = {r"\bROAD\b": "RD", r"\bSTREET\b": "ST", r"\bAVENUE\b": "AVE",
            r"\bDRIVE\b": "DR", r"\bLANE\b": "LN", r"\bCOURT\b": "CT",
            r"\bBOULEVARD\b": "BLVD", r"\bPLACE\b": "PL", r"\bTERRACE\b": "TER",
            r"\bCIRCLE\b": "CIR", r"\bHIGHWAY\b": "HWY"}
    for k, v in subs.items():
        s = re.sub(k, v, s)
    return re.sub(r"\s+", " ", s).strip()


def lookup_by_address(address: str, city: str = "", zip_code: str = "",
                      county: str = "", limit: int = 25) -> dict:
    """Fuzzy address lookup. Returns {match, candidates, count, confidence}."""
    norm = _norm_addr(address)
    m = re.match(r"^(\d+)\s+(\S+)", norm)
    if not m:
        return {"match": None, "candidates": [], "count": 0, "confidence": 0.0,
                "note": "could not parse a house-number + street from the address"}
    house, street1 = m.group(1), m.group(2)
    where = f"{F_ADDR} like '{house} {street1}%'"
    if zip_code:
        where += f" AND {F_ZIP}='{str(zip_code)[:5]}'"
    rows = _get({"$where": where, "$limit": limit})
    cands = [normalize(r) for r in rows]

    # score each candidate against the full normalized input
    target = _norm_addr(f"{address} {city} {zip_code}")
    scored = []
    for cand in cands:
        cand_str = _norm_addr(f"{cand['address']} {cand['city']} {cand['zip']}")
        s = SequenceMatcher(None, target, cand_str).ratio()
        if zip_code and cand["zip"] == str(zip_code)[:5]:
            s += 0.1
        if city and city.upper() in cand["city"].upper():
            s += 0.05
        if county and county.lower().replace(" county", "") in (cand["county"] or "").lower():
            s += 0.05
        scored.append((min(s, 1.0), cand))
    scored.sort(key=lambda x: x[0], reverse=True)

    best = scored[0] if scored else None
    return {
        "match": best[1] if best else None,
        "confidence": round(best[0], 3) if best else 0.0,
        "candidates": [c for _, c in scored],
        "count": len(scored),
    }


def find_property(address: str = "", city: str = "", zip_code: str = "",
                  county: str = "", account: str = "", parcel: str = "",
                  county_code: str = "") -> dict:
    """Unified entry point. Prefers the most precise identifier available."""
    if account:
        p = lookup_by_account(account)
        return {"match": p, "confidence": 1.0 if p else 0.0,
                "count": 1 if p else 0, "candidates": [p] if p else [],
                "method": "account"}
    if parcel and county_code:
        p = lookup_by_parcel(parcel, county_code)
        return {"match": p, "confidence": 1.0 if p else 0.0,
                "count": 1 if p else 0, "candidates": [p] if p else [],
                "method": "parcel"}
    if address:
        r = lookup_by_address(address, city, zip_code, county)
        r["method"] = "address"
        return r
    return {"match": None, "confidence": 0.0, "count": 0, "candidates": [],
            "method": "none", "note": "no usable identifier (address/account/parcel) provided"}


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1:
        print(json.dumps(find_property(address=" ".join(sys.argv[1:])), indent=2))
