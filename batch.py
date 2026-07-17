"""
Batch title research.

Feed a CSV list of properties (address, or account/parcel). For each row this:
  1. matches the property in SDAT (address -> deed reference + county),
  2. walks the title chain (reusing ONE logged-in MDLandRec session),
  3. runs the curative checks,
  4. writes a full report file AND one summary row.

Polite + resumable: throttled between properties, and re-running skips rows
already completed in the summary file, so a long list can be stopped/resumed.

Usage:
  python batch.py properties.csv                 # depth 3, resumes if summary exists
  python batch.py properties.csv --depth 4 --limit 20

Input CSV columns (use whatever you have; address is the main path):
  label, address, city, zip, county, account, parcel, county_code
"""
from __future__ import annotations

import argparse
import csv
import time
import traceback
from datetime import datetime
from pathlib import Path

import config
import sdat
import liens
from chain import walk_from
from client import MDLandRecClient
from curative import check_chain
from main import write_reports
from htmlreport import write_html_report
from models import BookPage, TitleReport

BATCH_DIR = config.OUTPUT_DIR / "batch"
PROPERTY_DELAY = 4.0            # seconds between properties (be a good citizen)

SUMMARY_FIELDS = [
    "label", "input_address", "status", "match_confidence",
    "matched_address", "city", "zip", "county", "liber", "folio",
    "total_assessment", "land_value", "improvement_value", "year_built",
    "sqft", "land_use", "last_sale_date", "last_sale_price",
    "deeds_walked", "critical", "warning", "review_info", "top_flags",
    "sdat_url", "report_file",
]


def _row_key(row: dict) -> str:
    return (row.get("account") or row.get("parcel") or
            f"{row.get('address','')}|{row.get('zip','')}").strip().lower()


def _load_done(summary_path: Path) -> set[str]:
    done: set[str] = set()
    if summary_path.exists():
        with summary_path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("status") in ("done", "no_match", "chain_error"):
                    done.add((r.get("label") or r.get("input_address", "")).strip().lower())
    return done


def process_row(client: MDLandRecClient, row: dict, depth: int, found: dict = None) -> dict:
    label = row.get("label") or row.get("address") or row.get("account") or "(unnamed)"
    out = {k: "" for k in SUMMARY_FIELDS}
    out["label"] = label
    out["input_address"] = row.get("address", "")

    if found is None:
        found = sdat.find_property(
            address=row.get("address", ""), city=row.get("city", ""),
            zip_code=row.get("zip", ""), county=row.get("county", ""),
            account=row.get("account", ""), parcel=row.get("parcel", ""),
            county_code=row.get("county_code", ""),
        )
    prop = found.get("match")
    out["match_confidence"] = found.get("confidence", 0)
    if not prop or not prop.get("book"):
        out["status"] = "no_match"
        return out

    out.update({
        "matched_address": prop["address"], "city": prop["city"], "zip": prop["zip"],
        "county": prop["county"], "liber": prop["book"], "folio": prop["page"],
        "total_assessment": prop["total_assessment"], "land_value": prop["land_value"],
        "improvement_value": prop["improvement_value"], "year_built": prop["year_built"],
        "sqft": prop["sqft"], "land_use": prop["land_use"],
        "last_sale_date": prop["last_sale_date"], "last_sale_price": prop["last_sale_price"],
        "sdat_url": prop["sdat_url"],
    })

    try:
        county = config.resolve_county(prop["county"])
        start = BookPage(county=county, book=prop["book"], page=prop["page"])
        chain = walk_from(client, start, depth)
        owner_names = chain[0].deed.grantee_names() if chain else []
        lf = liens.lien_flags(client, county, owner_names)
        flags = lf + check_chain(chain)
        report = TitleReport(county=county, start_reference=start, chain=chain, flags=flags)
        jp, tp = write_reports(report)
        hp = write_html_report(report)
        out["deeds_walked"] = len(chain)
        out["critical"] = sum(1 for f in flags if f.severity == "critical")
        out["warning"] = sum(1 for f in flags if f.severity == "warning")
        out["review_info"] = sum(1 for f in flags if f.severity in ("info", "review"))
        out["top_flags"] = "; ".join(
            f.code for f in flags if f.severity in ("critical", "warning"))[:300]
        out["report_file"] = str(hp)
        out["status"] = "done"
    except Exception as e:
        out["status"] = "chain_error"
        out["top_flags"] = f"{type(e).__name__}: {e}"[:300]
        traceback.print_exc()
    return out


def run_batch(csv_path: Path, depth: int, limit: int = 0) -> Path:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    summary_path = BATCH_DIR / f"summary_{csv_path.stem}_{stamp}.csv"
    done = _load_done(summary_path)
    if done:
        print(f"[batch] resuming - {len(done)} already-completed rows will be skipped")

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = [ {k.strip().lower(): (v or "").strip() for k, v in r.items()}
                 for r in csv.DictReader(f) ]

    # Which rows still need doing (skip completed, honour the limit).
    todo = []
    for row in rows:
        label = (row.get("label") or row.get("address") or "").strip().lower()
        if label in done:
            continue
        todo.append(row)
        if limit and len(todo) >= limit:
            break
    if not todo:
        print("[batch] nothing to do - everything in this list is already done")
        return summary_path

    # ---- Phase 1: match EVERY property in SDAT using ONE shared browser -------
    # (clears Cloudflare once for the whole list; avoids nesting Playwright.)
    print(f"[batch] matching {len(todo)} properties in SDAT (one browser)...")
    matched = []
    with sdat.SdatSession() as sess:
        sdat.ACTIVE_SESSION = sess
        try:
            for row in todo:
                found = sdat.find_property(
                    address=row.get("address", ""), city=row.get("city", ""),
                    zip_code=row.get("zip", ""), county=row.get("county", ""),
                    account=row.get("account", ""), parcel=row.get("parcel", ""),
                    county_code=row.get("county_code", ""),
                )
                matched.append((row, found))
                print(f"[match] {row.get('address')} -> confidence {found.get('confidence')}")
        finally:
            sdat.ACTIVE_SESSION = None

    # ---- Phase 2: walk every chain with ONE MDLandRec login ------------------
    new_file = not summary_path.exists()
    with MDLandRecClient() as client, summary_path.open("a", newline="", encoding="utf-8") as sf:
        writer = csv.DictWriter(sf, fieldnames=SUMMARY_FIELDS)
        if new_file:
            writer.writeheader()
        client.login()
        for i, (row, found) in enumerate(matched, 1):
            print(f"\n===== [{i}/{len(matched)}] {row.get('label') or row.get('address')} =====")
            result = process_row(client, row, depth, found=found)
            writer.writerow(result)
            sf.flush()                      # persist after every property (resumable)
            print(f"[batch] status={result['status']} "
                  f"deeds={result['deeds_walked']} critical={result['critical']}")
            time.sleep(PROPERTY_DELAY)

    print(f"\n[batch] done. Summary: {summary_path}")
    print(f"[batch] individual reports in: {config.OUTPUT_DIR}")
    return summary_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Batch Maryland title research from a CSV list")
    ap.add_argument("csv", help="input CSV (columns: label,address,city,zip,county,account,parcel,county_code)")
    ap.add_argument("--depth", type=int, default=config.DEFAULT_CHAIN_DEPTH)
    ap.add_argument("--limit", type=int, default=0, help="max NEW properties this run (0 = all)")
    a = ap.parse_args()
    run_batch(Path(a.csv), a.depth, a.limit)
