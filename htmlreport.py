"""Colorful standalone HTML report for a title chain (used by batch + single)."""
from __future__ import annotations

import html as _html
from datetime import datetime
from pathlib import Path

import config
from models import TitleReport


def _esc(x) -> str:
    return _html.escape(str(x)) if x is not None else "-"


def write_html_report(report: TitleReport) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = (f"{report.county.replace(' ', '_')}_{report.start_reference.book}_"
            f"{report.start_reference.page}_{stamp}")
    html_path = config.OUTPUT_DIR / f"{stem}.html"

    order = {"critical": 0, "warning": 1, "info": 2, "review": 3}
    crit = sum(1 for f in report.flags if f.severity == "critical")
    warn = sum(1 for f in report.flags if f.severity == "warning")

    deeds = []
    for link in report.chain:
        d = link.deed
        deeds.append(
            '<div class="deed"><div class="hd">#' + str(link.depth) + ' &middot; '
            + _esc(d.reference) + ' <span class="tag">[' + _esc(d.instrument_type or "deed")
            + ']</span></div>'
            + '<div class="mt"><b>Grantor:</b> ' + _esc(", ".join(d.grantor_names()) or "-")
            + ' &nbsp;&rarr;&nbsp; <b>Grantee:</b> ' + _esc(", ".join(d.grantee_names()) or "-")
            + '</div><div class="mt">Recorded ' + _esc(d.recorded_date or "-") + ' &middot; '
            + _esc(d.consideration or "-") + ' &middot; prior: ' + _esc(d.prior_reference or "-")
            + ' &middot; confidence ' + _esc(d.extraction_confidence) + '</div></div>')

    flags = []
    for f in sorted(report.flags, key=lambda x: order.get(x.severity, 4)):
        loc = (' <span class="loc">(deed #' + str(f.at_depth) + ')</span>'
               if f.at_depth is not None else "")
        flags.append('<div class="flag ' + _esc(f.severity) + '"><span class="cd">'
                     + _esc(f.code) + '</span>' + _esc(f.message) + loc + '</div>')

    css = (
        "body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "background:#f4f6f9;color:#1b2430}"
        "header{background:#0f2747;color:#fff;padding:16px 22px;display:flex;align-items:center;gap:12px}"
        "header .seal{width:32px;height:32px;border-radius:50%;background:#c9a227;display:flex;"
        "align-items:center;justify-content:center;color:#0f2747;font-weight:700}"
        ".wrap{max-width:1000px;margin:20px auto;padding:0 18px}"
        ".card{background:#fff;border:1px solid #e2e6ec;border-radius:12px;padding:18px;margin-bottom:16px}"
        "h2{font-size:16px;margin:0 0 10px;color:#0f2747}"
        ".meta{font-size:13px;color:#6b7480;line-height:1.8}"
        ".deed{border:1px solid #e2e6ec;border-left:4px solid #0f2747;border-radius:0 8px 8px 0;"
        "padding:11px 14px;margin:8px 0}"
        ".deed .hd{font-weight:700;color:#0f2747}.deed .tag{color:#6b7480;font-weight:400;font-size:12px}"
        ".deed .mt{font-size:13px;color:#5f6b7a;margin-top:3px}"
        ".pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;margin-right:6px}"
        ".pill.c{background:#fdecea;color:#c0392b}.pill.w{background:#fef6e7;color:#c77f1a}"
        ".flag{padding:8px 12px;border-radius:0 7px 7px 0;margin:6px 0;font-size:13px;border-left:4px solid}"
        ".flag.critical{background:#fdecea;border-color:#c0392b}"
        ".flag.warning{background:#fef6e7;border-color:#c77f1a}"
        ".flag.info,.flag.review{background:#eef1f5;border-color:#9aa4b1}"
        ".flag .cd{font-weight:700;margin-right:6px}.flag .loc{color:#6b7480;font-size:12px}"
        ".note{font-size:12px;color:#6b7480;margin-top:10px}"
    )

    doc = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Title report - ' + _esc(report.start_reference) + '</title><style>' + css
        + '</style></head><body>'
        '<header><div class="seal">MD</div><div>'
        '<div style="font-size:17px;font-weight:600">Maryland Title Research</div>'
        '<div style="color:#a9b6c9;font-size:12.5px">Chain of title + curative flags</div></div></header>'
        '<div class="wrap">'
        '<div class="card"><h2>' + _esc(report.start_reference) + '</h2>'
        '<div class="meta">County: ' + _esc(report.county) + '<br>Deeds walked: '
        + str(len(report.chain)) + '<br>Generated: ' + stamp + '</div></div>'
        '<div class="card"><h2>Chain of title (' + str(len(report.chain)) + ' deeds)</h2>'
        + ("".join(deeds) or '<div class="meta">No deeds retrieved.</div>') + '</div>'
        '<div class="card"><h2>Curative flags <span class="pill c">' + str(crit)
        + ' critical</span><span class="pill w">' + str(warn) + ' warning</span></h2>'
        + ("".join(flags) or '<div class="meta">No flags.</div>') + '</div>'
        '<div class="note">Automated first-pass aid. Not a certified title opinion. '
        'Verify all flagged items against the source images.</div>'
        '</div></body></html>'
    )
    html_path.write_text(doc, encoding="utf-8")
    return html_path
