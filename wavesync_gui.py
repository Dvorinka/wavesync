#!/usr/bin/env python3
"""
wavesync_gui.py - local web UI for wavesync. Zero dependencies beyond the
standard library (and wavesync's own numpy/scipy for autodetect helpers).

  python wavesync_gui.py [--port 8710] [--config wavesync.config.json]
  python wavesync.py --gui            # same thing

Serves a single-page UI on 127.0.0.1: browse folders, auto-detect mics,
edit the config, run scans with live logs, and review/block matches.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import wavesync

WAVESYNC_PY = Path(__file__).resolve().with_name("wavesync.py")
JOBS = {}
JOBS_LOCK = threading.Lock()


class Job:
    """A wavesync subprocess; stdout collected line-by-line for polling."""
    def __init__(self, argv, cwd):
        self.id = uuid.uuid4().hex[:8]
        self.argv = argv
        self.lines = []
        self.done = False
        self.code = None
        threading.Thread(target=self._run, args=(cwd,), daemon=True).start()

    def _run(self, cwd):
        try:
            p = subprocess.Popen(self.argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 cwd=cwd, bufsize=1,
                                 errors="replace")
            for line in p.stdout:
                self.lines.append(line.rstrip("\n"))
            self.code = p.wait()
        except Exception as e:
            self.lines.append(f"!! failed to start: {e}")
            self.code = -1
        self.done = True


def _drives():
    """Windows drive letters for the root of the folder picker."""
    out = []
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if os.path.isdir(c + ":\\"):
            out.append({"name": c + ":\\", "path": c + ":\\"})
    return out


def api_browse(path: str):
    """Directory listing for the folder picker modal."""
    if not path:
        if os.name == "nt":
            return {"path": "", "parent": "", "dirs": _drives()}
        path = str(Path.home())
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"error": f"not a directory: {p}"}
    dirs = []
    try:
        for d in sorted(p.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                n_media = sum(1 for i, f in enumerate(d.iterdir())
                              if i < 200 and f.is_file()
                              and (wavesync.is_video(f)
                                   or wavesync.is_audio(f)))
            except OSError:
                n_media = 0
            dirs.append({"name": d.name, "path": str(d),
                         "media": n_media})
    except OSError as e:
        return {"error": str(e)}
    return {"path": str(p), "parent": str(p.parent), "dirs": dirs}


def api_days(root: str):
    vi = Path(root).expanduser() if root else None
    days = wavesync.list_days(vi) if vi else []
    flat = bool(vi and vi.is_dir() and not days and any(
        wavesync.is_video(p) for p in vi.iterdir() if p.is_file()))
    counts = {}
    for d in days:
        counts[d] = len(wavesync.gather_videos(vi / d))
    out = {"days": days, "counts": counts, "flat": flat}
    if flat:
        out["flat_day"] = vi.name   # reports land under this name
    return out


def load_cfg(path: str):
    p = Path(path).expanduser()
    if not p.is_file():
        return None, f"config not found: {p}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON in {p}: {e}"


def work_dir_of(cfg_path: str) -> Path:
    cfg, _ = load_cfg(cfg_path)
    if cfg and cfg.get("work_dir"):
        return Path(cfg["work_dir"]).expanduser()
    return Path(cfg_path).expanduser().resolve().parent / "_wavesync"


def api_report(cfg_path: str, day: str):
    """Trimmed match table + current blocks for the review UI."""
    reports = work_dir_of(cfg_path) / "reports"
    jp = reports / f"{day}_sync.json"
    if not jp.is_file():
        return {"error": f"no report yet: {jp}"}
    results = json.loads(jp.read_text(encoding="utf-8"))
    blocks = {}
    bp = reports / f"{day}_blocks.json"
    if bp.is_file():
        blocks = json.loads(bp.read_text(encoding="utf-8"))
    rows = []
    for r in results:
        ms = []
        for m in r.get("matches", []):
            ms.append({k: m.get(k) for k in
                       ("mic", "wav_name", "shift_sec", "r_med", "z",
                        "cov_a", "cov_b", "selected", "nominated",
                        "time_ok", "drift_ms")})
        rows.append({"video": r["video_name"], "dur": r["video_dur"],
                     "matches": ms})
    return {"day": day, "rows": rows, "blocks": blocks}


def api_outputs(cfg_path: str):
    wd = work_dir_of(cfg_path)
    out = {"work_dir": str(wd), "timelines": [], "reports": [],
           "previews": []}
    for name, key in (("timelines", "timelines"), ("reports", "reports"),
                      ("previews", "previews")):
        d = wd / name
        if d.is_dir():
            out[key] = sorted(str(p) for p in d.rglob("*") if p.is_file())
    return out


def start_job(argv, cwd=None):
    job = Job(argv, cwd or str(Path.cwd()))
    with JOBS_LOCK:
        JOBS[job.id] = job
    return job.id


class Handler(BaseHTTPRequestHandler):
    cfg_default = wavesync.DEFAULT_CONFIG_NAME

    def log_message(self, *a):
        pass  # quiet; the UI shows scan logs, not HTTP noise

    # -- plumbing ------------------------------------------------------
    def _send(self, body, ctype="application/json", code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), "application/json", code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _qs(self):
        return {k: v[0] for k, v in
                parse_qs(urlparse(self.path).query).items()}

    # -- GET ------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = self._qs()
        try:
            if u.path == "/":
                self._send(INDEX_HTML, "text/html; charset=utf-8")
            elif u.path == "/api/browse":
                self._json(api_browse(q.get("path", "")))
            elif u.path == "/api/days":
                self._json(api_days(q.get("root", "")))
            elif u.path == "/api/config":
                cfg, err = load_cfg(q.get("path",
                                          self.cfg_default))
                self._json({"exists": cfg is not None,
                            "config": cfg, "error": err})
            elif u.path == "/api/report":
                self._json(api_report(q.get("config", self.cfg_default),
                                      q.get("day", "")))
            elif u.path == "/api/outputs":
                self._json(api_outputs(q.get("config",
                                             self.cfg_default)))
            elif u.path == "/api/jobs":
                with JOBS_LOCK:
                    self._json({"jobs": [
                        {"id": j.id, "cmd": " ".join(j.argv),
                         "done": j.done, "code": j.code,
                         "n_lines": len(j.lines)}
                        for j in JOBS.values()]})
            elif u.path == "/api/job":
                with JOBS_LOCK:
                    j = JOBS.get(q.get("id", ""))
                if not j:
                    self._json({"error": "no such job"}, 404)
                    return
                since = int(q.get("since", 0))
                self._json({"id": j.id, "lines": j.lines[since:],
                            "n": len(j.lines), "done": j.done,
                            "code": j.code})
            else:
                self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # -- POST -----------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        try:
            if u.path == "/api/autodetect":
                vr, ar = b.get("video_root"), b.get("audio_root")
                if not (vr and ar):
                    self._json({"error": "need video_root and audio_root"},
                               400)
                    return
                for k, v in (("video_root", vr), ("audio_root", ar)):
                    if not Path(v).expanduser().is_dir():
                        self._json({"error": f"{k} is not a directory: {v}"},
                                   400)
                        return
                wd = b.get("work_dir")
                cfg = wavesync.autodetect_config(
                    Path(vr).expanduser(), Path(ar).expanduser(),
                    Path(wd).expanduser() if wd else None)
                self._json({"config": cfg})
            elif u.path == "/api/save_config":
                path = Path(b.get("path", self.cfg_default)).expanduser()
                cfg = b.get("config")
                if not isinstance(cfg, dict):
                    self._json({"error": "config must be an object"}, 400)
                    return
                wavesync.write_config(cfg, path, force=True)
                self._json({"saved": str(path)})
            elif u.path == "/api/blocks":
                cfg_path = b.get("config", self.cfg_default)
                day = b.get("day")
                blocks = b.get("blocks", {})
                if not day:
                    self._json({"error": "need day"}, 400)
                    return
                reports = work_dir_of(cfg_path) / "reports"
                reports.mkdir(parents=True, exist_ok=True)
                bp = reports / f"{day}_blocks.json"
                bp.write_text(json.dumps(blocks, indent=1) + "\n",
                              encoding="utf-8")
                self._json({"saved": str(bp)})
            elif u.path == "/api/scan":
                cfg_path = b.get("config_path", self.cfg_default)
                if b.get("config") is not None:
                    wavesync.write_config(
                        b["config"], Path(cfg_path).expanduser(),
                        force=True)
                argv = [sys.executable, str(WAVESYNC_PY)]
                if b.get("selftest"):
                    argv += ["--selftest"]
                else:
                    day = b.get("day") or "all"
                    argv += [day, "--config", cfg_path]
                    if b.get("timeline_only"):
                        argv.append("--timeline-only")
                    if b.get("preview"):
                        argv.append("--preview")
                    if b.get("video_range"):
                        argv += ["--video-range", b["video_range"]]
                    if b.get("only_video"):
                        argv += ["--only-video", b["only_video"]]
                jid = start_job(argv)
                self._json({"job": jid, "cmd": " ".join(argv)})
            else:
                self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<link rel="icon" href="data:,">
<title>wavesync</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--panel:#161b22;--edge:#2d333b;--fg:#e6edf3;
--dim:#8b949e;--acc:#4c8dff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
h1{font-size:18px;padding:14px 20px;margin:0;border-bottom:1px solid var(--edge);
display:flex;gap:10px;align-items:baseline}
h1 span{color:var(--dim);font-size:12px}
main{display:grid;grid-template-columns:380px 1fr;gap:14px;padding:14px 20px}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:8px;
padding:14px;margin-bottom:14px}
.card h2{margin:0 0 10px;font-size:13px;text-transform:uppercase;
letter-spacing:.06em;color:var(--dim)}
label{display:block;font-size:12px;color:var(--dim);margin:8px 0 3px}
input[type=text],select{width:100%;background:#0d1117;border:1px solid var(--edge);
color:var(--fg);border-radius:6px;padding:7px 9px;font:13px ui-monospace,monospace}
input[type=text]:focus{outline:none;border-color:var(--acc)}
.row{display:flex;gap:8px}.row>*{flex:1}
button{background:#21262d;border:1px solid var(--edge);color:var(--fg);
border-radius:6px;padding:7px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--acc)}
button.acc{background:var(--acc);border-color:var(--acc);color:#fff}
button:disabled{opacity:.45;cursor:default}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:5px 7px;border-bottom:1px solid var(--edge);
white-space:nowrap}
th{color:var(--dim);font-weight:600}
td input[type=text],td input[type=number]{width:100%;padding:3px 6px;
font-size:12px;background:transparent;border:1px solid transparent}
td input:focus{border-color:var(--acc)}
#log{background:#010409;border:1px solid var(--edge);border-radius:8px;
height:340px;overflow:auto;padding:10px 12px;font:12px/1.5 ui-monospace,monospace;
white-space:pre-wrap}
.muted{color:var(--dim)}.ok{color:var(--ok)}.warn{color:var(--warn)}
.bad{color:var(--bad)}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
border:1px solid var(--edge);color:var(--dim)}
.pill.sel{border-color:var(--ok);color:var(--ok)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;
align-items:center;justify-content:center;z-index:9}
.modal.open{display:flex}
.modal .box{background:var(--panel);border:1px solid var(--edge);
border-radius:10px;width:560px;max-height:70vh;display:flex;flex-direction:column}
.modal .hd{padding:10px 14px;border-bottom:1px solid var(--edge);
font:12px ui-monospace,monospace;color:var(--dim);word-break:break-all}
.modal .ls{overflow:auto;padding:6px}
.modal .ls div{padding:6px 10px;border-radius:6px;cursor:pointer}
.modal .ls div:hover{background:#21262d}
.modal .ft{padding:10px 14px;border-top:1px solid var(--edge);
display:flex;gap:8px;justify-content:flex-end}
.tag{font-size:11px;color:var(--warn)}
</style></head><body>
<h1>wavesync <span>external audio &rarr; camera sync &rarr; Resolve FCPXML</span>
<span id="ver" class="muted" style="margin-left:auto"></span></h1>
<main>
<div>
<div class="card"><h2>Project</h2>
<label>Video root (day folders, or flat)</label>
<div class="row"><input id="video_root" type=text placeholder="/media/camera">
<button onclick="browse('video_root')">Browse</button></div>
<label>Audio root (one folder per recorder, or flat)</label>
<div class="row"><input id="audio_root" type=text placeholder="/media/recorders">
<button onclick="browse('audio_root')">Browse</button></div>
<label>Work dir (reports, timelines, cache)</label>
<div class="row"><input id="work_dir" type=text placeholder="/media/_sync">
<button onclick="browse('work_dir')">Browse</button></div>
<label>Config file</label>
<input id="cfg_path" type=text value="wavesync.config.json">
<div class="row" style="margin-top:10px">
<button class="acc" onclick="autodetect()">Auto-detect</button>
<button onclick="loadCfg()">Load</button>
<button onclick="saveCfg()">Save config</button></div>
<div id="cfgmsg" class="muted" style="margin-top:8px"></div>
</div>
<div class="card"><h2>Recorders / mics</h2>
<table id="mics"><thead><tr><th>name</th><th>folder</th><th>lane</th>
<th>rolling</th><th></th></tr></thead><tbody></tbody></table>
<button style="margin-top:8px" onclick="addMic()">+ add mic</button>
<label>Analysis filter (ffmpeg -af)</label>
<input id="analysis_filter" type=text value="dynaudnorm=f=250:g=15:m=25">
<div class="row"><div><label>Sample rate</label>
<input id="sample_rate" type=text value="8000"></div>
<div><label>Min r (timeline)</label>
<input id="min_r" type=text value="0.20"></div></div>
<label>Skip dirs (comma separated)</label>
<input id="skip_dirs" type=text>
<label>Skip prefixes</label>
<input id="skip_prefixes" type=text>
</div>
<div class="card"><h2>Scan</h2>
<label>Day</label><select id="days" size="6"></select>
<div class="row" style="margin-top:6px">
<label style="flex:1"><input type=checkbox id="alldays"> all days</label>
<label style="flex:1"><input type=checkbox id="timeline_only"> timeline only</label>
<label style="flex:1"><input type=checkbox id="preview"> previews</label></div>
<label>Video range (e.g. C0357-C0380, optional)</label>
<input id="video_range" type=text>
<div class="row" style="margin-top:10px">
<button class="acc" onclick="scan()">Run scan</button>
<button onclick="selftest()">Self-test</button>
<button onclick="outputs()">Outputs</button></div>
<div id="scanmsg" class="muted" style="margin-top:8px"></div>
</div>
</div>
<div>
<div class="card"><h2>Log <span id="jobstate" class="pill"></span></h2>
<div id="log"><span class="muted">wavesync ready. Pick folders, Auto-detect, Run scan.</span></div></div>
<div class="card"><h2>Matches <span id="repday" class="muted"></span></h2>
<div class="row" style="margin-bottom:8px">
<button onclick="loadReport()">Load report for selected day</button>
<button onclick="saveBlocks()">Save blocks</button>
<button onclick="scan(true)">Rebuild timeline</button></div>
<div style="overflow:auto"><table id="rep"><thead><tr>
<th>video</th><th>mic</th><th>audio file</th><th>shift</th><th>r</th>
<th>covers</th><th>state</th><th>block</th></tr></thead><tbody>
<tr><td colspan=8 class=muted>run a scan, then load the report</td></tr>
</tbody></table></div></div>
</div>
</main>
<div class="modal" id="modal"><div class="box">
<div class="hd" id="mpath"></div><div class="ls" id="mlist"></div>
<div class="ft"><button onclick="closeModal()">cancel</button>
<button class="acc" onclick="pickHere()">select this folder</button></div>
</div></div>
<script>
let CFG=null, JOB=null, JOBN=0, BLOCKS={};
const $=id=>document.getElementById(id);
async function api(path, body){
  const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body)}:{};
  const r=await fetch(path,opt); return r.json();}
function say(el,msg,cls){$(el).textContent=msg;$(el).className=cls||'muted';}
function log(line){const l=$('log');
  l.insertAdjacentHTML('beforeend',line.replace(/&/g,'&amp;')
  .replace(/</g,'&lt;')+'\n');l.scrollTop=l.scrollHeight;}

let BTARGET=null;
async function browse(target){
  BTARGET=target;const cur=$(target).value||'';
  openModal(cur);}
async function openModal(path){
  const r=await api('/api/browse?path='+encodeURIComponent(path));
  $('modal').classList.add('open');
  if(r.error){$('mlist').innerHTML='<div class=bad>'+r.error+'</div>';
    $('mpath').textContent=path||'/';return;}
  $('mpath').textContent=r.path||'/';
  let h='';
  if(r.parent&&r.parent!==r.path)
    h+='<div onclick="openModal(\''+esc(r.parent)+'\')">.. (up)</div>';
  for(const d of r.dirs)
    h+='<div onclick="openModal(\''+esc(d.path)+'\')">'+d.name+'/'+
       (d.media?' <span class=muted>· '+d.media+' media</span>':'')+'</div>';
  if(!r.dirs.length)h+='<div class=muted>(no subfolders)</div>';
  $('mlist').innerHTML=h;}
function esc(s){return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
function pickHere(){if(BTARGET)$(BTARGET).value=$('mpath').textContent;
  closeModal();refreshDays();}
function closeModal(){$('modal').classList.remove('open');}

function collectCfg(){
  const mics={};
  document.querySelectorAll('#mics tbody tr').forEach(tr=>{
    const t=tr.querySelectorAll('input');
    if(t[0].value)mics[t[0].value]={folder:t[1].value||t[0].value,
      lane:parseInt(t[2].value)||-1,rolling:t[3].checked};});
  return {video_root:$('video_root').value,audio_root:$('audio_root').value,
    work_dir:$('work_dir').value||'./_wavesync',mics:mics,
    skip_dirs:$('skip_dirs').value.split(',').map(s=>s.trim()).filter(Boolean),
    skip_prefixes:$('skip_prefixes').value.split(',')
      .map(s=>s.trim()).filter(Boolean),
    analysis_filter:$('analysis_filter').value,
    sample_rate:parseInt($('sample_rate').value)||8000,
    min_r_timeline:parseFloat($('min_r').value)||0.2};}
function fillCfg(c){
  $('video_root').value=c.video_root||'';$('audio_root').value=c.audio_root||'';
  $('work_dir').value=c.work_dir||'';
  $('analysis_filter').value=c.analysis_filter||'dynaudnorm=f=250:g=15:m=25';
  $('sample_rate').value=c.sample_rate||8000;
  $('min_r').value=c.min_r_timeline??0.2;
  $('skip_dirs').value=(c.skip_dirs||[]).join(', ');
  $('skip_prefixes').value=(c.skip_prefixes||[]).join(', ');
  const tb=document.querySelector('#mics tbody');tb.innerHTML='';
  Object.entries(c.mics||{}).forEach(([n,m])=>addMic(n,m));
  refreshDays();}
function addMic(n,m){n=n||'';m=m||{};
  const tr=document.createElement('tr');
  tr.innerHTML='<td><input type=text value="'+n+'"></td>'+
   '<td><input type=text value="'+(m.folder||n)+'"></td>'+
   '<td><input type=text size=3 value="'+(m.lane??-1)+'"></td>'+
   '<td><input type=checkbox '+(m.rolling?'checked':'')+'></td>'+
   '<td><button onclick="this.closest(\'tr\').remove()">x</button></td>';
  document.querySelector('#mics tbody').appendChild(tr);}

async function autodetect(){
  const v=$('video_root').value,a=$('audio_root').value;
  if(!v||!a){say('cfgmsg','set video + audio roots first','warn');return;}
  say('cfgmsg','detecting...');
  const r=await api('/api/autodetect',{video_root:v,audio_root:a,
    work_dir:$('work_dir').value});
  if(r.error){say('cfgmsg',r.error,'bad');return;}
  CFG=r.config;fillCfg(CFG);
  say('cfgmsg','detected '+Object.keys(CFG.mics).length+' mic(s)','ok');}
async function loadCfg(){
  const r=await api('/api/config?path='+encodeURIComponent($('cfg_path').value));
  if(r.config){CFG=r.config;fillCfg(CFG);say('cfgmsg','loaded','ok');}
  else say('cfgmsg',r.error||'no config yet - Auto-detect makes one','warn');}
async function saveCfg(){
  CFG=collectCfg();
  const r=await api('/api/save_config',{path:$('cfg_path').value,config:CFG});
  say('cfgmsg',r.error||'saved '+r.saved,r.error?'bad':'ok');}
async function refreshDays(){
  const r=await api('/api/days?root='+encodeURIComponent($('video_root').value));
  const sel=$('days');sel.innerHTML='';
  for(const d of r.days){const o=document.createElement('option');
    o.value=d;o.textContent=d+' ('+(r.counts[d]||0)+' clips)';
    sel.appendChild(o);}
  if(r.flat){const o=document.createElement('option');
    o.value=r.flat_day;o.textContent=r.flat_day+' (flat folder)';
    sel.appendChild(o);}}

async function scan(tlOnly){
  CFG=collectCfg();
  const day=$('alldays').checked?'all':($('days').value||'all');
  const body={config_path:$('cfg_path').value,config:CFG,day:day,
    timeline_only:tlOnly||$('timeline_only').checked,
    preview:$('preview').checked,video_range:$('video_range').value};
  say('scanmsg','starting...');
  const r=await api('/api/scan',body);
  if(r.error){say('scanmsg',r.error,'bad');return;}
  JOB=r.job;JOBN=0;$('log').innerHTML='';
  say('scanmsg','job '+r.job+': '+r.cmd,'muted');poll();}
async function selftest(){
  const r=await api('/api/scan',{selftest:true});
  JOB=r.job;JOBN=0;$('log').innerHTML='';poll();}
async function poll(){
  if(!JOB)return;
  const r=await api('/api/job?id='+JOB+'&since='+JOBN);
  (r.lines||[]).forEach(log);JOBN=r.n;
  $('jobstate').textContent=r.done?('exit '+r.code):'running';
  $('jobstate').className='pill '+(r.done&&r.code===0?'sel':'');
  if(!r.done)setTimeout(poll,600);
  else say('scanmsg',r.code===0?'done':'failed (exit '+r.code+')',
           r.code===0?'ok':'bad');}
async function loadReport(){
  const d=[...$('days').selectedOptions].map(o=>o.value)[0]||
    $('days').options[0]&&$('days').options[0].value;
  if(!d){say('scanmsg','pick a day first','warn');return;}
  const r=await api('/api/report?config='+encodeURIComponent($('cfg_path').value)
    +'&day='+encodeURIComponent(d));
  if(r.error){say('scanmsg',r.error,'warn');return;}
  $('repday').textContent='· '+d;BLOCKS=r.blocks||{};
  const tb=document.querySelector('#rep tbody');tb.innerHTML='';
  for(const row of r.rows){
    const vb=BLOCKS[row.video]||[];
    if(!row.matches.length){tb.insertAdjacentHTML('beforeend',
      '<tr><td>'+row.video+'</td><td colspan=6 class=muted>no match</td>'+
      blkCell(row.video,'*',vb.includes('*'))+'</tr>');continue;}
    for(const m of row.matches){
      const base=(m.wav_name||'').replace(/ \[(CONT|GUESS)\]$/,'');
      const st=m.selected?'<span class="pill sel">selected</span>'
        :'<span class=pill>'+(m.nominated||'rejected')+'</span>';
      tb.insertAdjacentHTML('beforeend','<tr><td>'+row.video+'</td><td>'
        +(m.mic||'')+'</td><td>'+(m.wav_name||'')+'</td><td>'
        +(m.shift_sec!=null?m.shift_sec.toFixed(2)+'s':'')+'</td><td>'
        +(m.r_med!=null?m.r_med.toFixed(2):'')+'</td><td>'
        +(m.cov_a!=null?Math.round(m.cov_a)+'-'+Math.round(m.cov_b)+'s':'')
        +'</td><td>'+st+(m.time_ok===false?' <span class=tag>TIME</span>':'')
        +'</td>'+blkCell(row.video,m.wav_name,vb.includes(base))+'</tr>');}}}
function blkCell(video,wav,checked){
  return '<td><input type=checkbox data-v="'+video+'" data-w="'+wav+'"'
    +(checked?' checked':'')+'></td>';}
async function saveBlocks(){
  const d=$('repday').textContent.replace('· ','').trim();
  if(!d)return;
  const blocks={};
  document.querySelectorAll('#rep input[type=checkbox]:checked')
    .forEach(cb=>{(blocks[cb.dataset.v]=blocks[cb.dataset.v]||[])
      .push(cb.dataset.w);});
  const r=await api('/api/blocks',{config:$('cfg_path').value,day:d,
    blocks:blocks});
  say('scanmsg',r.error||('blocks saved - rebuild timeline to apply'),
    r.error?'bad':'ok');}
async function outputs(){
  const r=await api('/api/outputs?config='+encodeURIComponent($('cfg_path').value));
  log('work_dir: '+r.work_dir);
  (r.timelines||[]).forEach(t=>log('  timeline: '+t));
  (r.reports||[]).forEach(t=>log('  report:   '+t));}
$('video_root').addEventListener('change',refreshDays);
loadCfg();
</script></body></html>
"""


def serve(port: int = 8710, config: str = None):
    if config:
        Handler.cfg_default = config
    url = f"http://127.0.0.1:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"wavesync UI on {url}  (Ctrl+C to stop)")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="wavesync local web UI")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--config", default=None,
                    help="default config path shown in the UI")
    args = ap.parse_args()
    serve(port=args.port, config=args.config)


if __name__ == "__main__":
    main()
