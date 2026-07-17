"""
Maryland Title Research - local web app.

Run this file (or double-click START_APP.bat); it starts a small local server
and you use the tool in your browser. Nothing is hosted or shared - it runs on
your machine with your own .env credentials.
"""
from __future__ import annotations

import io
import threading
import traceback
import uuid
from contextlib import redirect_stdout
from pathlib import Path

from flask import Flask, request, jsonify, send_file, abort

import config
import sdat
import client as client_mod
from client import MDLandRecClient
from chain import walk_from
from curative import check_chain
from main import write_reports
from htmlreport import write_html_report
from models import BookPage, TitleReport
import batch as batch_mod

app = Flask(__name__)
JOBS: dict[str, dict] = {}
CURRENT_JOB_ID = {"id": None}


# --- access-code (2FA) hook: pause the job, let the browser + UI handle it ----
def _access_code_hook():
    job = JOBS.get(CURRENT_JOB_ID["id"])
    if not job:
        return
    job["needs_code"] = True
    job["continue_event"].wait()
    job["continue_event"].clear()
    job["needs_code"] = False


client_mod.ACCESS_CODE_HOOK = _access_code_hook


class _LogWriter(io.TextIOBase):
    def __init__(self, job):
        self.job = job
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.job["log"].append(line.rstrip())
        return len(s)


def _new_job(kind: str) -> str:
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "kind": kind, "status": "running", "log": [],
                 "result": None, "error": None, "needs_code": False,
                 "continue_event": threading.Event()}
    return jid


def _run(jid: str, target):
    job = JOBS[jid]
    CURRENT_JOB_ID["id"] = jid
    with redirect_stdout(_LogWriter(job)):
        try:
            job["result"] = target(job)
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
    CURRENT_JOB_ID["id"] = None


# --- serialization helpers ----------------------------------------------------
def _deed_dict(link):
    d = link.deed
    return {
        "depth": link.depth, "reference": str(d.reference),
        "instrument_type": d.instrument_type,
        "grantors": d.grantor_names(), "grantees": d.grantee_names(),
        "recorded_date": str(d.recorded_date) if d.recorded_date else None,
        "consideration": d.consideration,
        "prior": str(d.prior_reference) if d.prior_reference else None,
        "confidence": d.extraction_confidence,
    }


def _flag_dict(f):
    return {"severity": f.severity, "code": f.code, "message": f.message,
            "at_depth": f.at_depth}


# --- job bodies ---------------------------------------------------------------
def _single_job(form):
    def body(job):
        prop = None
        conf = None
        book = (form.get("book") or "").strip()
        page = (form.get("page") or "").strip()
        county = (form.get("county") or "").strip()
        if not (book and page):                       # look up by address
            found = sdat.find_property(
                address=form.get("address", ""), city=form.get("city", ""),
                zip_code=form.get("zip", ""), county=county)
            prop = found.get("match")
            conf = found.get("confidence")
            if not prop or not prop.get("book"):
                return {"property": None, "match_confidence": conf, "chain": [],
                        "flags": [], "report_file": None,
                        "note": "No SDAT match for that address."}
            book, page, county = prop["book"], prop["page"], prop["county"]

        canon = config.resolve_county(county)
        start = BookPage(county=canon, book=book, page=page)
        with MDLandRecClient() as c:
            c.login()
            chain = walk_from(c, start, int(form.get("depth", 3)))
        flags = check_chain(chain)
        report = TitleReport(county=canon, start_reference=start, chain=chain, flags=flags)
        jp, tp = write_reports(report)
        hp = write_html_report(report)
        return {
            "property": prop, "match_confidence": conf,
            "chain": [_deed_dict(l) for l in chain],
            "flags": [_flag_dict(f) for f in flags],
            "report_file": Path(hp).name,
        }
    return body


def _batch_job(csv_path: Path, depth: int):
    def body(job):
        summary = batch_mod.run_batch(csv_path, depth)
        import csv as _csv
        rows = list(_csv.DictReader(open(summary, encoding="utf-8")))
        return {"summary_csv": Path(summary).name, "rows": rows}
    return body


# --- routes -------------------------------------------------------------------
@app.route("/")
def index():
    return HTML


@app.route("/api/search", methods=["POST"])
def api_search():
    if CURRENT_JOB_ID["id"]:
        return jsonify({"error": "A search is already running."}), 409
    form = request.get_json(force=True)
    jid = _new_job("single")
    threading.Thread(target=_run, args=(jid, _single_job(form)), daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/batch", methods=["POST"])
def api_batch():
    if CURRENT_JOB_ID["id"]:
        return jsonify({"error": "A run is already in progress."}), 409
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No CSV uploaded."}), 400
    up = config.DATA_DIR / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    csv_path = up / f.filename
    f.save(csv_path)
    depth = int(request.form.get("depth", 3))
    jid = _new_job("batch")
    threading.Thread(target=_run, args=(jid, _batch_job(csv_path, depth)), daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/status/<jid>")
def api_status(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "no such job"}), 404
    return jsonify({"status": job["status"], "log": job["log"][-400:],
                    "needs_code": job["needs_code"], "result": job["result"],
                    "error": job["error"]})


@app.route("/api/continue/<jid>", methods=["POST"])
def api_continue(jid):
    job = JOBS.get(jid)
    if job:
        job["continue_event"].set()
    return jsonify({"ok": True})


@app.route("/api/report/<name>")
def api_report(name):
    p = (config.OUTPUT_DIR / name).resolve()
    if not str(p).startswith(str(config.OUTPUT_DIR.resolve())) or not p.exists():
        abort(404)
    mt = "text/html" if p.suffix.lower() == ".html" else "text/plain"
    return send_file(p, mimetype=mt)


@app.route("/api/summary/<name>")
def api_summary(name):
    p = (config.OUTPUT_DIR / "batch" / name).resolve()
    if not str(p).startswith(str((config.OUTPUT_DIR / "batch").resolve())) or not p.exists():
        abort(404)
    return send_file(p, as_attachment=True)


HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maryland Title Research</title>
<style>
:root{--navy:#0f2747;--gold:#c9a227;--bg:#f4f6f9;--line:#e2e6ec;
--crit:#c0392b;--warn:#c77f1a;--ok:#2e7d46;--muted:#6b7480;}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
background:var(--bg);color:#1b2430}
header{background:var(--navy);color:#fff;padding:18px 26px;display:flex;align-items:center;gap:14px}
header .seal{width:34px;height:34px;border-radius:50%;background:var(--gold);display:flex;
align-items:center;justify-content:center;color:var(--navy);font-weight:800}
header h1{font-size:19px;margin:0;font-weight:650}header .sub{color:#a9b6c9;font-size:12.5px;margin-top:2px}
.wrap{max-width:1000px;margin:22px auto;padding:0 18px}
.tabs{display:flex;gap:6px;margin-bottom:16px}
.tab{padding:9px 16px;border-radius:8px 8px 0 0;background:#e7ebf1;cursor:pointer;font-weight:600;font-size:14px}
.tab.on{background:#fff;color:var(--navy);box-shadow:0 -2px 0 var(--gold) inset}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
label{display:block;font-size:12.5px;color:var(--muted);margin:10px 0 4px;font-weight:600}
input,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;font-size:14px}
.row{display:flex;gap:12px}.row>div{flex:1}
button{background:var(--navy);color:#fff;border:0;padding:11px 20px;border-radius:8px;font-size:14.5px;
font-weight:650;cursor:pointer;margin-top:14px}button:hover{background:#16345c}
button.gold{background:var(--gold);color:var(--navy)}
.hint{font-size:12px;color:var(--muted);margin-top:6px}
.log{background:#0b1a2e;color:#cfe3ff;font-family:ui-monospace,Consolas,monospace;font-size:12px;
padding:12px;border-radius:8px;height:200px;overflow:auto;white-space:pre-wrap;display:none}
.deed{border:1px solid var(--line);border-left:4px solid var(--navy);border-radius:8px;padding:12px 14px;margin:8px 0}
.deed .hd{font-weight:700;color:var(--navy)}.deed .mt{font-size:12.5px;color:var(--muted);margin-top:3px}
.flag{padding:8px 12px;border-radius:7px;margin:6px 0;font-size:13px;border-left:4px solid}
.flag.critical{background:#fdecea;border-color:var(--crit)}.flag.warning{background:#fef6e7;border-color:var(--warn)}
.flag.info,.flag.review{background:#eef1f5;border-color:#9aa4b1}
.flag .cd{font-weight:700;margin-right:6px}
.pill{display:inline-block;padding:3px 9px;border-radius:20px;font-size:12px;font-weight:700;margin-right:6px}
.pill.c{background:#fdecea;color:var(--crit)}.pill.w{background:#fef6e7;color:var(--warn)}.pill.o{background:#e8f5ec;color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:12px}.code{background:#eef1f5;border-radius:6px;padding:12px;font-size:12.5px;color:#333}
.banner{background:#fff8e1;border:1px solid var(--gold);border-radius:8px;padding:12px 14px;margin-top:12px;font-size:13.5px;display:none}
a{color:var(--navy)}.small{font-size:12px;color:var(--muted)}
</style></head><body>
<header><div class="seal">MD</div><div><h1>Maryland Title Research</h1>
<div class="sub">Chain of title + curative flags &middot; runs locally on your machine</div></div></header>
<div class="wrap">
  <div class="tabs">
    <div class="tab on" data-t="single" onclick="tab('single')">Single Property</div>
    <div class="tab" data-t="batch" onclick="tab('batch')">Batch List</div>
  </div>

  <div id="single" class="pane">
    <div class="card">
      <label>Property address</label>
      <input id="address" placeholder="2150 Lake Dr">
      <div class="row">
        <div><label>City</label><input id="city" placeholder="Pasadena"></div>
        <div><label>ZIP</label><input id="zip" placeholder="21122"></div>
        <div><label>County</label><input id="county" placeholder="Anne Arundel"></div>
      </div>
      <div class="hint">Or skip the address and enter a known deed reference:</div>
      <div class="row">
        <div><label>Liber (book)</label><input id="book" placeholder="7230"></div>
        <div><label>Folio (page)</label><input id="page" placeholder="156"></div>
        <div><label>Deeds back</label><select id="depth">
          <option>2</option><option selected>3</option><option>4</option><option>5</option></select></div>
      </div>
      <button onclick="runSingle()">Run title search</button>
      <div id="s_banner" class="banner"></div>
    </div>
    <div id="s_log" class="log"></div>
    <div id="s_out"></div>
  </div>

  <div id="batch" class="pane" style="display:none">
    <div class="card">
      <label>Your property list (CSV)</label>
      <input type="file" id="csv" accept=".csv">
      <div class="hint">Columns: label, address, city, zip, county, account, parcel, county_code.
        ZIP codes improve matching. See properties_TEMPLATE.csv.</div>
      <div class="row"><div><label>Deeds back per property</label><select id="bdepth">
        <option>2</option><option selected>3</option><option>4</option></select></div><div></div><div></div></div>
      <button onclick="runBatch()">Run batch</button>
      <div id="b_banner" class="banner"></div>
    </div>
    <div id="b_log" class="log"></div>
    <div id="b_out"></div>
  </div>
</div>
<script>
function tab(t){document.querySelectorAll('.pane').forEach(p=>p.style.display='none');
document.getElementById(t).style.display='block';
document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x.dataset.t===t));}
let job=null,poll=null,pre='s';
function val(id){return document.getElementById(id).value.trim();}
function runSingle(){pre='s';start('/api/search',{address:val('address'),city:val('city'),zip:val('zip'),
county:val('county'),book:val('book'),page:val('page'),depth:val('depth')},null);}
function runBatch(){pre='b';const f=document.getElementById('csv').files[0];
if(!f){alert('Pick a CSV file first.');return;}
const fd=new FormData();fd.append('file',f);fd.append('depth',val('bdepth'));start('/api/batch',null,fd);}
function start(url,jsonBody,fd){
document.getElementById(pre+'_out').innerHTML='';
const lg=document.getElementById(pre+'_log');lg.style.display='block';lg.textContent='Starting...\n';
document.getElementById(pre+'_banner').style.display='none';
const opt=fd?{method:'POST',body:fd}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(jsonBody)};
fetch(url,opt).then(r=>r.json()).then(d=>{if(d.error){lg.textContent+=d.error;return;}
job=d.job_id;poll=setInterval(check,1200);});}
function check(){fetch('/api/status/'+job).then(r=>r.json()).then(d=>{
const lg=document.getElementById(pre+'_log');lg.textContent=d.log.join('\n');lg.scrollTop=lg.scrollHeight;
const bn=document.getElementById(pre+'_banner');
if(d.needs_code){bn.style.display='block';
bn.innerHTML='<b>Access code needed.</b> A browser window opened - check your email, type the MDLandRec code into that window and submit it there, then click: <button class="gold" onclick="cont()">I\'ve entered it - continue</button>';}
else{bn.style.display='none';}
if(d.status==='done'){clearInterval(poll);render(d.result);}
if(d.status==='error'){clearInterval(poll);lg.textContent+='\n\nERROR: '+d.error;}});}
function cont(){fetch('/api/continue/'+job,{method:'POST'});
document.getElementById(pre+'_banner').style.display='none';}
function esc(s){return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function render(res){
if(pre==='s')renderSingle(res);else renderBatch(res);}
function renderSingle(res){const o=document.getElementById('s_out');if(!res){o.innerHTML='';return;}
let h='';
if(res.note){h+='<div class="card">'+esc(res.note)+'</div>';}
if(res.property){const p=res.property;h+='<div class="card"><h3 style="margin:0 0 8px">'+esc(p.address)+', '+esc(p.city)+' '+esc(p.zip)+'</h3>'+
'<div class="small">'+esc(p.county)+' &middot; match confidence '+(res.match_confidence)+'</div>'+
'<table style="margin-top:10px"><tr><th>Deed ref</th><th>Assessment</th><th>Land / Improv</th><th>Built</th><th>Sq ft</th><th>Last sale</th></tr>'+
'<tr><td>Liber '+esc(p.book)+' Folio '+esc(p.page)+'</td><td>$'+esc(p.total_assessment)+'</td><td>$'+esc(p.land_value)+' / $'+esc(p.improvement_value)+'</td><td>'+esc(p.year_built)+'</td><td>'+esc(p.sqft)+'</td><td>'+esc(p.last_sale_date)+' ($'+esc(p.last_sale_price)+')</td></tr></table>'+
(p.sdat_url?'<div class="small" style="margin-top:8px"><a href="'+p.sdat_url+'" target="_blank">View on SDAT &rarr;</a></div>':'')+'</div>';}
if(res.chain&&res.chain.length){h+='<div class="card"><h3 style="margin:0 0 10px">Chain of title ('+res.chain.length+' deeds)</h3>';
res.chain.forEach(d=>{h+='<div class="deed"><div class="hd">#'+d.depth+' &middot; '+esc(d.reference)+' <span class="small">['+esc(d.instrument_type||'deed')+']</span></div>'+
'<div class="mt"><b>Grantor:</b> '+esc(d.grantors.join(', ')||'-')+' &nbsp; <b>&rarr; Grantee:</b> '+esc(d.grantees.join(', ')||'-')+'</div>'+
'<div class="mt">Recorded '+esc(d.recorded_date||'-')+' &middot; '+esc(d.consideration||'-')+' &middot; prior: '+esc(d.prior||'-')+' &middot; confidence '+esc(d.confidence)+'</div></div>';});h+='</div>';}
if(res.flags){const c=res.flags.filter(f=>f.severity==='critical'),w=res.flags.filter(f=>f.severity==='warning'),r=res.flags.filter(f=>f.severity==='info'||f.severity==='review');
h+='<div class="card"><h3 style="margin:0 0 10px">Curative flags '+
'<span class="pill c">'+c.length+' critical</span><span class="pill w">'+w.length+' warning</span></h3>';
[...c,...w,...r].forEach(f=>{h+='<div class="flag '+f.severity+'"><span class="cd">'+esc(f.code)+'</span>'+esc(f.message)+(f.at_depth!=null?' <span class="small">(deed #'+f.at_depth+')</span>':'')+'</div>';});h+='</div>';}
if(res.report_file){h+='<div class="card"><a href="/api/report/'+encodeURIComponent(res.report_file)+'" target="_blank">Open full report &rarr;</a></div>';}
o.innerHTML=h;}
function renderBatch(res){const o=document.getElementById('b_out');if(!res){return;}
let h='<div class="card"><h3 style="margin:0 0 8px">Batch results ('+res.rows.length+' properties)</h3>'+
'<div style="margin-bottom:10px"><a class="pill o" href="/api/summary/'+encodeURIComponent(res.summary_csv)+'">&darr; Download summary CSV</a></div>'+
'<table><tr><th>Property</th><th>Status</th><th>County</th><th>Liber/Folio</th><th>Deeds</th><th>Crit</th><th>Warn</th><th>Report</th></tr>';
res.rows.forEach(r=>{h+='<tr><td>'+esc(r.label||r.input_address)+'</td><td>'+esc(r.status)+'</td><td>'+esc(r.county)+'</td>'+
'<td>'+esc(r.liber)+'/'+esc(r.folio)+'</td><td>'+esc(r.deeds_walked)+'</td>'+
'<td>'+(r.critical>0?'<b style="color:#c0392b">'+esc(r.critical)+'</b>':esc(r.critical))+'</td><td>'+esc(r.warning)+'</td>'+
'<td>'+(r.report_file?'<a href="/api/report/'+encodeURIComponent(r.report_file.split(/[\\\\/]/).pop())+'" target="_blank">open</a>':'-')+'</td></tr>';});
h+='</table></div>';o.innerHTML=h;}
</script></body></html>"""


if __name__ == "__main__":
    import webbrowser
    port = 5000
    print(f"\n  Maryland Title Research is running.")
    print(f"  Open your browser to:  http://127.0.0.1:{port}\n")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
