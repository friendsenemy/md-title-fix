"""
Deed extractor - reads deed folio PDFs with Claude vision and returns a
structured Deed PLUS an "instrument_complete" signal used to decide whether to
pull the next folio.

Each MDLandRec folio is one PDF page. A deed usually spans several consecutive
folios; the reference points at the first. We feed the pages gathered so far
and ask the model whether the instrument that STARTS on the first page is fully
contained in them yet.
"""
from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic

import config
from models import BookPage, Deed, Party

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are a Maryland title examiner's assistant. You read scanned
recorded land-record pages (PDFs) and extract structured facts about the ONE
instrument that BEGINS on the first page provided. Recorded instruments run over
consecutive folios; a new instrument starts with a fresh caption/parties/date and
its own recording stamp. You never guess: if a field is illegible or absent, use
null. You carefully separate the GRANTOR (party conveying) from the GRANTEE
(party receiving), and you find the PRIOR REFERENCE - the liber/folio citation of
the deed by which the current grantor previously acquired the property (often
'being the same property conveyed ... recorded in Liber ___ folio ___')."""

INSTRUCTIONS = """Extract JSON with EXACTLY these keys:

{
  "instrument_type": string|null,
  "recorded_date": "YYYY-MM-DD"|null,
  "execution_date": "YYYY-MM-DD"|null,
  "grantors": [{"name": str, "marital_status": str|null}],
  "grantees": [{"name": str, "marital_status": str|null}],
  "consideration": string|null,
  "legal_description": string|null,
  "prior_reference": {"book": str, "page": str}|null,
  "recited_encumbrances": [str],
  "instrument_complete": boolean,   // TRUE only if the instrument that begins on
                                    // page 1 CONCLUDES within the pages provided
                                    // (signatures + acknowledgment/notary, or a
                                    // clearly new instrument starts after it).
                                    // FALSE if it appears to continue past the
                                    // last page provided.
  "confidence": number,             // 0..1
  "notes": string|null
}

Return ONLY the JSON object."""


def _pdf_block(path: str) -> dict:
    data = base64.standard_b64encode(Path(path).read_bytes()).decode()
    return {"type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data}}


def extract(county: str, book: str, page: str, pdf_paths: list[str]) -> tuple[Deed, bool]:
    """Return (Deed, instrument_complete) for the deed starting on pdf_paths[0]."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    content: list[dict] = [{"type": "text", "text": INSTRUCTIONS}]
    for p in pdf_paths[:12]:
        content.append(_pdf_block(p))
    resp = client.messages.create(
        model=MODEL, max_tokens=2000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = _first_text(resp)
    data = _loads_lenient(text)
    deed = _to_deed(county, book, page, pdf_paths, data)
    return deed, bool(data.get("instrument_complete", True))



def _first_text(resp) -> str:
    """Return the text of the first text block (skips thinking/other blocks)."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    for block in resp.content:
        if hasattr(block, "text"):
            return block.text
    return ""

def _to_deed(county, book, page, pdf_paths, data) -> Deed:
    prior = None
    pr = data.get("prior_reference")
    if pr and pr.get("book") and pr.get("page"):
        # Sanity-check the prior liber: real Maryland book numbers are short.
        # A garbled OCR read (e.g. an 8-digit "33593232") would send us chasing a
        # nonexistent volume, so flag it and stop the chain gracefully instead.
        digits = "".join(ch for ch in str(pr["book"]) if ch.isdigit())
        if digits and len(digits) <= 6:
            prior = BookPage(county=county, book=str(pr["book"]), page=str(pr["page"]))
        else:
            data["notes"] = ((data.get("notes") or "")
                + f" [prior reference {pr.get('book')}/{pr.get('page')} looks "
                  "unreadable/implausible; chain-back stopped here - verify manually]")
    return Deed(
        reference=BookPage(county=county, book=str(book), page=str(page)),
        instrument_type=data.get("instrument_type"),
        recorded_date=_date(data.get("recorded_date")),
        execution_date=_date(data.get("execution_date")),
        grantors=[Party(name=g["name"], role="grantor", marital_status=g.get("marital_status"))
                  for g in data.get("grantors", []) if g.get("name")],
        grantees=[Party(name=g["name"], role="grantee", marital_status=g.get("marital_status"))
                  for g in data.get("grantees", []) if g.get("name")],
        legal_description=data.get("legal_description"),
        consideration=data.get("consideration"),
        prior_reference=prior,
        recited_encumbrances=data.get("recited_encumbrances", []) or [],
        image_paths=list(pdf_paths),
        extraction_confidence=data.get("confidence"),
        notes=data.get("notes"),
    )


def _loads_lenient(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s:e + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"confidence": 0.0, "instrument_complete": True,
                "notes": "JSON parse failed", "grantors": [], "grantees": []}


def _date(s: Optional[str]):
    if not s:
        return None
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)
    except Exception:
        return None
