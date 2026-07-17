"""
Curative checker - rules over an extracted chain that flag common
"messy title" problems a Maryland examiner would want to run down.

Two layers:
  1. AUTO detectors below flag what is visible in the deed image/chain text.
  2. A compact per-category REVIEW reminder lists the defect classes that need
     a separate records pull or manual inspection (see CURATIVE_CHECKLIST.md for
     the full itemized taxonomy).

These are HEURISTICS. A flag means "a human should look," not "defective title."
Name matching is fuzzy on purpose - deeds spell names inconsistently.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from models import ChainLink, CurativeFlag


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    n = name.upper()
    n = re.sub(r"[.,]", " ", n)
    for suffix in (" JR", " SR", " III", " II", " IV", " ET UX", " ET AL"):
        n = n.replace(suffix, " ")
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# Words that describe a ROLE or an entity/relationship, not the party's identity.
# We drop these before comparing names so "Successor Trustee of the Smith Trust"
# still links to "Smith Trust", and "aka"/entity variations don't look like breaks.
_ROLE_STOP = {"TRUSTEE", "TRUSTEES", "SUCCESSOR", "PERSONAL", "REPRESENTATIVE",
              "REP", "EXECUTOR", "EXECUTRIX", "ADMINISTRATOR", "ADMINISTRATRIX",
              "ATTORNEY", "FACT", "AGENT", "GUARDIAN", "CONSERVATOR", "ESQ",
              "ESQUIRE", "AKA", "FKA", "NKA", "KNOWN", "RECORD"}
_ENTITY_STOP = {"THE", "OF", "AND", "FOR", "TO", "UNDER", "DATED", "TRUST",
                "REVOCABLE", "IRREVOCABLE", "LIVING", "FAMILY", "ESTATE", "LLC",
                "INC", "LP", "LLP", "LTD", "COMPANY", "CORP", "CORPORATION",
                "ASSOCIATION", "PARTNERSHIP", "ET", "UX", "AL", "HIS", "HER",
                "WIFE", "HUSBAND", "TENANTS", "JOINT", "ENTIRETY", "SURVIVOR"}


def _core_tokens(name: str) -> set:
    """Distinctive name tokens - surnames, trust names, entity names - with
    role/relationship words and short/number tokens stripped out."""
    out = set()
    for raw in _norm(name).split():
        t = re.sub(r"[^A-Z]", "", raw)
        if len(t) >= 3 and t not in _ROLE_STOP and t not in _ENTITY_STOP:
            out.add(t)
    return out


def _link(a: str, b: str, threshold: float = 0.82) -> bool:
    """True if two party names plausibly refer to the same person/family/entity:
    either the full names are similar, OR they share a distinctive token (a
    surname, or a trust/entity name). This is what stops successor-trustee,
    a.k.a., and entity-name variations from being flagged as chain breaks."""
    if _similar(a, b) >= threshold:
        return True
    return bool(_core_tokens(a) & _core_tokens(b))


def _any_match(names_a: list[str], names_b: list[str], threshold: float = 0.82) -> bool:
    return any(_link(a, b, threshold) for a in names_a for b in names_b)


def _matches_any(name: str, names: list[str], threshold: float = 0.82) -> bool:
    return any(_link(name, n, threshold) for n in names)


def _blob(deed) -> str:
    """All searchable text we have for a deed, lowercased."""
    parts = [
        deed.instrument_type or "",
        deed.legal_description or "",
        deed.consideration or "",
        deed.notes or "",
        " ".join(deed.recited_encumbrances or []),
        " ".join(deed.grantor_names()),
        " ".join(deed.grantee_names()),
    ]
    return " ".join(parts).lower()


_ENTITY_RE = re.compile(
    r"\b(llc|l\.l\.c|inc\b|incorporated|corp\b|corporation|company|co\.|"
    r"lp\b|llp\b|ltd|limited|trust|trustee|partnership|association)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Per-deed automated detectors
# ---------------------------------------------------------------------------
def _check_deed(link: ChainLink) -> list[CurativeFlag]:
    d = link.deed
    depth = link.depth
    blob = _blob(d)
    flags: list[CurativeFlag] = []

    def add(sev, code, msg):
        flags.append(CurativeFlag(severity=sev, code=code, message=msg, at_depth=depth))

    # Extraction quality
    if d.extraction_confidence is not None and d.extraction_confidence < 0.6:
        add("warning", "LOW_CONFIDENCE",
            f"Extraction confidence {d.extraction_confidence:.2f}; verify this deed manually.")

    # Missing parties
    if not d.grantors:
        add("critical", "NO_GRANTOR", "No grantor extracted.")
    if not d.grantees:
        add("critical", "NO_GRANTEE", "No grantee extracted.")

    # Recited encumbrances. Skip generic boilerplate ("subject to easements,
    # covenants and restrictions of record"); then split what remains into real
    # liens (need a release) vs. restrictions/agreements (run with the land).
    _enc_real = ("mortgage", "deed of trust", "lien", "judgment", "judgement",
                 "assessment", "liber", "folio", "$", "promissory",
                 "security instrument", "ucc", "note recorded")
    _enc_boiler = ("of record", "matters of record", "easements, covenants",
                   "covenants and restrictions", "restrictions of record",
                   "subject to all easements", "rights of way", "zoning")
    _enc_lien = ("mortgage", "deed of trust", "lien", "judgment", "judgement",
                 "security instrument", "promissory note", "financing statement")
    for enc in d.recited_encumbrances:
        e = (enc or "").lower()
        if not any(k in e for k in _enc_real) and any(b in e for b in _enc_boiler):
            continue
        if any(k in e for k in _enc_lien):
            add("warning", "RECITED_LIEN",
                f"Recites a lien/mortgage: '{enc}'. Confirm it was released.")
        else:
            add("info", "RECITED_RESTRICTION",
                f"Recites a recorded restriction/agreement: '{enc}'. It runs with the "
                "land - review its terms (noted, not necessarily a defect).")

    # Estate / probate transfer [#38/#46/#47]
    gblob = " ".join(d.grantor_names()).upper()
    if any(k in gblob for k in ("ESTATE OF", "PERSONAL REPRESENTATIVE", "PERSONAL REP",
                                "EXECUTOR", "EXECUTRIX", "ADMINISTRATOR", "ADMINISTRATRIX")):
        add("warning", "ESTATE_TRANSFER",
            "Grantor appears to be an estate/PR. Verify letters, closure, and heirs.")

    # Power of attorney execution [#16]
    if any(k in blob for k in ("attorney-in-fact", "attorney in fact", "power of attorney",
                               " poa ", "as agent for")):
        add("warning", "POA_EXECUTION",
            "Deed executed under a power of attorney. Verify the POA was recorded, "
            "valid, and not revoked at execution.")

    # Guardian / conservator [#17]
    if any(k in blob for k in ("guardian", "conservator", "committee for", "next friend")):
        add("warning", "GUARDIAN_CONSERVATOR",
            "Signed by a guardian/conservator. Confirm court authority to convey.")

    # Quitclaim deed [#43]
    if "quitclaim" in blob or "quit claim" in blob or "quit-claim" in blob:
        add("info", "QUITCLAIM",
            "Quitclaim deed - conveys only whatever interest the grantor had; scrutinize.")

    # Trustee / foreclosure / judicial / sheriff / tax sale [#40/#41/#42/#83/#90/#91]
    if any(k in blob for k in ("substitute trustee", "trustee's deed", "trustees deed",
                               "foreclosure", "sheriff", "judicial sale", "tax sale",
                               "tax deed", "trustee's sale")):
        add("warning", "FORECLOSURE_OR_SALE_DEED",
            "Trustee/foreclosure/sheriff/tax-sale deed. Verify notice, authority, and "
            "sale regularity.")

    # MERS [#41]
    if "mers" in blob or "mortgage electronic" in blob:
        add("info", "MERS_IN_CHAIN",
            "MERS appears in the chain. Verify assignment authority to foreclose/convey.")

    # Correction / confirmatory / scrivener [#84/#23]
    if any(k in blob for k in ("correction deed", "corrective deed", "confirmatory",
                               "scrivener", "re-record", "rerecord")):
        add("info", "CORRECTION_DEED",
            "Correction/confirmatory deed. Confirm it doesn't contradict prior records "
            "or change substance.")

    # Entity grantor - verify good standing/authority [#14/#44/#45/#87]
    for g in d.grantors:
        if _ENTITY_RE.search(g.name):
            add("info", "ENTITY_GRANTOR",
                f"Grantor '{g.name}' is a business entity/trust. Verify good standing "
                "and signer authority at the time of conveyance.")
            if any(k in blob for k in ("dissolved", "forfeited", "revoked", "inactive")):
                add("critical", "DISSOLVED_ENTITY",
                    f"Grantor '{g.name}' may be a dissolved/forfeited entity.")
            break

    # Life estate / remainder [#62/#63/#64]
    if any(k in blob for k in ("life estate", "for life", "life tenant", "remainderman",
                               "remainder to", "reversion")):
        add("warning", "LIFE_ESTATE_OR_REMAINDER",
            "Life-estate / remainder / reversion language. Confirm all interest holders "
            "joined the conveyance.")

    # Nominal consideration - gift / possible fraudulent or estate transfer [#93/#13].
    # Skip routine cases: transfer into the owner's OWN trust/LLC (grantor and
    # grantee are the same party) or a contribution for a membership/ownership
    # interest - normal estate-planning / entity moves, not red flags.
    cons = (d.consideration or "").lower()
    if cons and any(k in cons for k in ("$0", "$1.00", "$1 ", "one dollar", "love and affection",
                                        "nominal", "gift", "no consideration")):
        _self_transfer = _any_match(d.grantor_names(), d.grantee_names())
        _entity_move = any(k in cons for k in ("membership interest", "member interest",
                                               "capital contribution", "ownership interest",
                                               "in exchange for"))
        if not _self_transfer and not _entity_move:
            add("info", "NOMINAL_CONSIDERATION",
                f"Nominal/no consideration ({d.consideration}). Possible gift, intra-family, "
                "or fraudulent-transfer scenario.")

    # Defective / missing legal description [#6]
    ld = (d.legal_description or "").strip()
    if not ld:
        add("warning", "NO_LEGAL_DESCRIPTION", "No legal description extracted.")
    elif len(ld) < 25:
        add("warning", "WEAK_LEGAL_DESCRIPTION",
            f"Very short legal description ({len(ld)} chars); may be defective.")
    elif not any(k in ld.lower() for k in ("lot", "block", "liber", "folio", "plat",
                                           "parcel", "metes", "bound", "section", "acre")):
        add("info", "VAGUE_LEGAL_DESCRIPTION",
            "Legal description lacks lot/block/plat/metes-and-bounds markers; verify it "
            "identifies the parcel unambiguously.")

    return flags


# ---------------------------------------------------------------------------
# Cross-deed (chain continuity) detectors
# ---------------------------------------------------------------------------
def _check_continuity(chain: list[ChainLink]) -> list[CurativeFlag]:
    flags: list[CurativeFlag] = []
    for i in range(len(chain) - 1):
        cur = chain[i].deed
        prev = chain[i + 1].deed

        # Broken chain [#5]
        cur_grantors = cur.grantor_names()
        prev_grantees = prev.grantee_names()
        d_cur, d_prev = chain[i].depth, chain[i + 1].depth

        if not _any_match(cur_grantors, prev_grantees):
            flags.append(CurativeFlag(
                severity="critical", code="CHAIN_BREAK", at_depth=d_cur,
                message=f"Grantor(s) of deed #{d_cur} "
                        f"({', '.join(cur_grantors) or '-'}) do not match grantee(s) "
                        f"of prior deed #{d_prev} "
                        f"({', '.join(prev_grantees) or '-'}). Possible break in "
                        "chain of title or a missing conveyance."))
        else:
            # Dropped grantee: someone who RECEIVED title in the prior deed is not
            # a grantor in the later conveyance (death/survivorship, divorce, gap).
            for gn in prev_grantees:
                if not _matches_any(gn, cur_grantors):
                    flags.append(CurativeFlag(
                        severity="warning", code="DROPPED_GRANTEE", at_depth=d_cur,
                        message=f"'{gn}' received title in deed #{d_prev} but is NOT a "
                                f"grantor in deed #{d_cur}. Confirm how that interest was "
                                "divested - death/survivorship (tenancy by entirety or "
                                "joint tenancy), divorce decree, or a missing conveyance."))
            # Extra grantor: someone conveys in the later deed who did NOT receive
            # title in the prior deed - their source of interest is not in the chain.
            for gn in cur_grantors:
                if not _matches_any(gn, prev_grantees):
                    flags.append(CurativeFlag(
                        severity="warning", code="EXTRA_GRANTOR", at_depth=d_cur,
                        message=f"'{gn}' conveys in deed #{d_cur} but did NOT receive "
                                f"title in prior deed #{d_prev}. Source of their interest "
                                "is not shown - check for an omitted/unrecorded "
                                "conveyance, inheritance, or partial-interest transfer."))

        # Name-spelling drift [#24]
        for gn in cur.grantor_names():
            for pn in prev.grantee_names():
                r = _similar(gn, pn)
                if 0.82 <= r < 0.97:
                    flags.append(CurativeFlag(
                        severity="info", code="NAME_VARIANT", at_depth=chain[i].depth,
                        message=f"Name spelling differs between deeds: '{gn}' vs '{pn}' "
                                f"(similarity {r:.2f}). May warrant a confirmatory/"
                                "scrivener's affidavit."))

        # Rapid resale / possible flip or straw transaction [#93/#117]
        if cur.recorded_date and prev.recorded_date:
            gap = (cur.recorded_date - prev.recorded_date).days
            if 0 <= gap <= 90:
                flags.append(CurativeFlag(
                    severity="info", code="RAPID_RESALE", at_depth=chain[i].depth,
                    message=f"Only {gap} days between deed #{chain[i+1].depth} and "
                            f"#{chain[i].depth}. Check for a flip / straw-buyer / "
                            "double-escrow pattern."))

    return flags


# ---------------------------------------------------------------------------
# Compact per-category REVIEW reminders (records/manual layer).
# Full itemized taxonomy lives in CURATIVE_CHECKLIST.md.
# ---------------------------------------------------------------------------
_REVIEW_CATEGORIES = [
    ("LIENS", "Pull liens: mortgages/releases, judgments, IRS, HOA (incl. super-lien), "
              "mechanic's, municipal/code, environmental, PACE, Medicaid, child-support, "
              "UCC fixtures, water/sewer & special assessments."),
    ("COURT", "Search dockets: lis pendens, quiet-title, partition, condemnation, "
              "bankruptcy (stay), federal forfeiture, pending litigation."),
    ("PROBATE", "Confirm any estate is fully administered; check for omitted/pretermitted "
                "heirs, competing claims, stale orders, jurisdiction."),
    ("ENTITY", "Verify entity grantors are in good standing with authority to convey "
               "(dissolved/forfeited LLCs, inactive corps, land-trust beneficiaries)."),
    ("SURVEY", "Order/verify survey: boundaries, encroachments, access/landlocked, "
               "easements (recorded & unrecorded), acreage & plat conformity."),
    ("EXECUTION", "Inspect execution: signatures vs. chain, notary commission valid at "
                  "date, acknowledgment/witness sufficiency, RON validity, POA effective."),
    ("MARITAL", "Confirm spousal joinder / dower-curtesy / homestead where a married or "
                "single-stated grantor conveyed alone."),
    ("SPECIAL", "Watch mineral/riparian severances, restrictive covenants, reversionary & "
                "life-estate interests, TOD/beneficiary deeds, redemption rights."),
]


def review_checklist_flags() -> list[CurativeFlag]:
    out = [CurativeFlag(severity="review", code="MANUAL_REVIEW",
                        message="Records/manual defect classes to run down for this "
                                "property (see CURATIVE_CHECKLIST.md for the full list):")]
    for code, msg in _REVIEW_CATEGORIES:
        out.append(CurativeFlag(severity="review", code=code, message=msg))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def check_chain(chain: list[ChainLink], include_checklist: bool = True) -> list[CurativeFlag]:
    if not chain:
        return [CurativeFlag(severity="critical", code="EMPTY_CHAIN",
                             message="No deeds were retrieved.")]

    flags: list[CurativeFlag] = []
    for link in chain:
        flags.extend(_check_deed(link))
    flags.extend(_check_continuity(chain))

    # Chain end / dead-end prior reference [#chain]
    if chain[-1].deed.prior_reference is None:
        flags.append(CurativeFlag(
            severity="info", code="CHAIN_END", at_depth=chain[-1].depth,
            message="Last deed has no readable prior reference - chain-back stopped here."))

    if include_checklist:
        flags.extend(review_checklist_flags())
    return flags
