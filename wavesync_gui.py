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
    for name in ("timelines", "reports", "previews"):
        d = wd / name
        if d.is_dir():
            out[name] = sorted(str(p) for p in d.rglob("*") if p.is_file())
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
                cfg, err = load_cfg(q.get("path", self.cfg_default))
                self._json({"exists": cfg is not None,
                            "config": cfg, "error": err})
            elif u.path == "/api/report":
                self._json(api_report(q.get("config", self.cfg_default),
                                      q.get("day", "")))
            elif u.path == "/api/outputs":
                self._json(api_outputs(q.get("config", self.cfg_default)))
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
/* Hallmark · redesign · genre: professional NLE tool · Resolve-adjacent
 * pre-emit critique: P5 H4 E5 S4 R5 V4 */
:root{
  --bg:#141414; --panel:#1d1d1d; --panel2:#222222; --inset:#0f0f0f;
  --edge:#0a0a0a; --edge-hi:#2e2e2e; --divider:#242424;
  --fg:#d2d2d2; --dim:#8c8c8c; --faint:#585858;
  --acc:#d9832b; --acc-hi:#f09a3e; --acc-dim:#7a4d1c;
  --teal:#3d9b94; --red:#c05555;
  --sel:#31404f; --sel-edge:#4a6a8a;
  --sans:-apple-system,"Segoe UI",Inter,Roboto,system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Consolas,monospace;
}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);font:12px/1.45 var(--sans);
  -webkit-font-smoothing:antialiased;overflow:hidden;display:flex;
  flex-direction:column}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#383838;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#4a4a4a}

/* ---- top bar ---------------------------------------------------- */
#topbar{height:38px;flex:0 0 auto;display:flex;align-items:center;
  padding:0 12px;background:#191919;border-bottom:1px solid var(--edge);
  gap:14px;user-select:none}
#topbar .mark{display:flex;align-items:center;gap:8px}
#topbar .mark svg{display:block}
#topbar .name{font-weight:600;letter-spacing:.14em;font-size:11px;
  text-transform:uppercase}
#topbar .sub{color:var(--faint);font-size:10.5px;letter-spacing:.04em}
#topbar .sp{flex:1}
.chip{display:inline-flex;align-items:center;gap:6px;height:22px;
  padding:0 9px;border:1px solid var(--edge-hi);background:var(--panel2);
  font:10px var(--mono);color:var(--dim);letter-spacing:.03em}
.dot{width:7px;height:7px;border-radius:50%;background:var(--faint)}
.dot.run{background:var(--acc);animation:pulse 1s infinite}
.dot.ok{background:var(--teal)}
.dot.err{background:var(--red)}
@keyframes pulse{50%{opacity:.35}}

/* ---- layout ------------------------------------------------------ */
#body{flex:1;display:flex;min-height:0}
#left{width:352px;flex:0 0 auto;overflow-y:auto;border-right:1px solid var(--edge);
  background:var(--bg);padding:10px}
#right{flex:1;display:flex;flex-direction:column;min-width:0}
#logwrap{flex:1.15;min-height:120px;display:flex;flex-direction:column;
  border-bottom:1px solid var(--edge)}
#repwrap{flex:1;min-height:120px;display:flex;flex-direction:column}

.panel{background:var(--panel);border:1px solid var(--edge);
  border-top:1px solid var(--edge-hi);margin-bottom:10px}
.panel>header{display:flex;align-items:center;justify-content:space-between;
  padding:6px 10px;border-bottom:1px solid var(--divider);
  font-size:10px;letter-spacing:.12em;color:var(--dim);
  text-transform:uppercase;user-select:none}
.panel>.body{padding:10px}

label.f{display:block;font-size:9.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);margin:8px 0 3px}
label.f:first-child{margin-top:0}

input[type=text],input[type=number]{width:100%;height:24px;
  background:var(--inset);border:1px solid var(--edge);
  border-top-color:#000;color:var(--fg);padding:0 7px;
  font:11px var(--mono);outline:none}
input[type=text]:focus,input[type=number]:focus{border-color:var(--acc-dim);
  box-shadow:inset 0 0 0 1px var(--acc-dim)}
input::placeholder{color:var(--faint)}
.igroup{display:flex}
.igroup input{flex:1;min-width:0}
.igroup button{border-left:none;border-radius:0}

button{height:24px;padding:0 10px;background:#2b2b2b;
  border:1px solid var(--edge);border-top-color:#383838;color:var(--fg);
  font:11px var(--sans);cursor:pointer;border-radius:2px;
  transition:background .08s,border-color .08s}
button:hover:not(:disabled){background:#343434}
button:active:not(:disabled){transform:scale(.98)}
button:disabled{opacity:.4;cursor:default}
button.acc{background:var(--acc);border-color:#8a4f14;color:#171006;
  font-weight:600;letter-spacing:.02em}
button.acc:hover:not(:disabled){background:var(--acc-hi)}
button.ghost{background:transparent;border-color:transparent;
  color:var(--dim)}
button.ghost:hover:not(:disabled){color:var(--fg);background:#2b2b2b}
.btnrow{display:flex;gap:6px;margin-top:10px}
.btnrow>*{flex:1}

/* checkboxes */
.cbx{display:flex;align-items:center;gap:7px;font-size:11px;
  color:var(--dim);cursor:pointer;user-select:none;padding:2px 0}
.cbx input{display:none}
.cbx .box{width:13px;height:13px;background:var(--inset);
  border:1px solid #3a3a3a;display:inline-flex;align-items:center;
  justify-content:center;flex:0 0 auto}
.cbx input:checked+.box{background:var(--acc);border-color:var(--acc)}
.cbx input:checked+.box::after{content:"";width:7px;height:4px;
  border-left:2px solid #171006;border-bottom:2px solid #171006;
  transform:rotate(-45deg) translateY(-1px)}
.cbxrow{display:flex;gap:14px;margin-top:8px}

/* mic table */
table{border-collapse:collapse;width:100%}
#mics th{font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);text-align:left;padding:4px 6px;
  border-bottom:1px solid var(--divider);font-weight:600}
#mics td{padding:3px 4px;border-bottom:1px solid #1a1a1a}
#mics input[type=text],#mics input[type=number]{height:20px;
  font-size:11px;background:transparent;border-color:transparent;
  padding:0 4px}
#mics input[type=text]:focus,#mics input[type=number]:focus{
  background:var(--inset);border-color:var(--acc-dim);box-shadow:none}
#mics tr:hover td{background:#232323}
.lanechip{display:inline-block;width:9px;height:9px;margin-right:2px;
  vertical-align:-1px}
.xbtn{padding:0 6px;height:20px;font-size:11px;color:var(--faint);
  background:transparent;border-color:transparent}
.xbtn:hover{color:var(--red);background:transparent}

/* day list */
#daylist{border:1px solid var(--edge);background:var(--inset);
  max-height:150px;overflow-y:auto}
.drow{display:flex;align-items:center;padding:5px 8px;cursor:pointer;
  border-bottom:1px solid #161616;font:11px var(--mono);color:var(--dim);
  border-left:2px solid transparent;user-select:none}
.drow:hover{background:#1c1c1c;color:var(--fg)}
.drow.sel{background:var(--sel);color:#fff;border-left-color:var(--acc)}
.drow .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drow .c{color:var(--faint);font-size:10px}
.drow.sel .c{color:#9db4cc}
.dempty{padding:8px;color:var(--faint);font:10.5px var(--sans)}

/* console */
#log{flex:1;overflow-y:auto;background:var(--inset);padding:8px 10px;
  font:11px/1.55 var(--mono);color:#b8b8b8;white-space:pre-wrap;
  word-break:break-all}
#log .t{color:var(--faint)}
#log .l-acc{color:var(--acc)}
#log .l-bad{color:var(--red)}
.panel .toolbar{display:flex;gap:4px}
.panel .toolbar button{height:18px;padding:0 7px;font-size:10px}

/* match table */
#rep-scroll{flex:1;overflow:auto}
#rep{font-size:11px}
#rep thead th{position:sticky;top:0;background:#1a1a1a;z-index:2;
  font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);text-align:left;padding:6px 8px;font-weight:600;
  border-bottom:1px solid var(--edge);white-space:nowrap}
#rep td{padding:5px 8px;border-bottom:1px solid #1c1c1c;
  white-space:nowrap;vertical-align:middle}
#rep tr.mrow:hover td{background:#22262b}
#rep td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;
  text-align:right}
#rep th.num{text-align:right}
#rep td.name{font-family:var(--mono);font-size:10.5px}
.vhead td{background:#181818;font-family:var(--mono);font-size:10.5px;
  color:var(--dim);border-bottom:1px solid var(--divider);
  border-top:1px solid #2a2a2a;padding:6px 8px;letter-spacing:.02em}
.vhead .vd{float:right;color:var(--faint)}
.pill{display:inline-block;height:16px;line-height:16px;padding:0 6px;
  font-size:9px;letter-spacing:.08em;text-transform:uppercase;
  border:1px solid #3a3a3a;color:var(--dim)}
.pill.sel{color:var(--teal);border-color:#2c5a56}
.pill.review{color:var(--acc);border-color:var(--acc-dim)}
.pill.blocked{color:var(--faint);text-decoration:line-through}
.tag-time{color:var(--red);font-size:9px;letter-spacing:.08em;
  margin-left:5px}
.empty{color:var(--faint);padding:14px;font-size:11px}
.msg{font-size:10.5px;min-height:14px;margin-top:7px}
.msg.err{color:var(--red)}.msg.ok{color:var(--teal)}.msg.info{color:var(--dim)}

/* status bar */
#statusbar{height:22px;flex:0 0 auto;display:flex;align-items:center;
  gap:16px;padding:0 12px;background:#191919;
  border-top:1px solid var(--edge);font:10px var(--mono);
  color:var(--faint);user-select:none}
#statusbar .st{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#statusbar .right{margin-left:auto;display:flex;gap:16px}

/* modal */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
  align-items:center;justify-content:center;z-index:9}
.modal.open{display:flex}
.modal .box{background:#1c1c1c;border:1px solid var(--edge);
  border-top:1px solid var(--edge-hi);width:520px;max-height:64vh;
  display:flex;flex-direction:column}
.modal .hd{padding:7px 10px;border-bottom:1px solid var(--divider);
  font:10.5px var(--mono);color:var(--dim);word-break:break-all;
  background:#191919}
.modal .ls{overflow-y:auto;padding:3px}
.modal .ls .drow{font-size:11.5px;border-left:none;padding:6px 9px}
.modal .ft{padding:8px 10px;border-top:1px solid var(--divider);
  display:flex;gap:8px;justify-content:flex-end;background:#191919}
</style></head><body>

<div id="topbar">
  <div class="mark">
    <svg width="17" height="17" viewBox="0 0 17 17"><g fill="none"
      stroke="#d9832b" stroke-width="1.4" stroke-linecap="round">
      <path d="M2 8.5h2M13 8.5h2"/>
      <path d="M5.5 5v7M8.5 2.5v12M11.5 5v7"/></g></svg>
    <span class="name">wavesync</span>
    <span class="sub">RECORDER &rarr; CAMERA &rarr; RESOLVE</span>
  </div>
  <div class="sp"></div>
  <span class="chip"><span id="jobdot" class="dot"></span>
    <span id="jobstate">idle</span></span>
  <span class="chip" id="ver">v0.2.0</span>
</div>

<div id="body">
<div id="left">

  <div class="panel"><header>Project</header><div class="body">
    <label class="f">Video root</label>
    <div class="igroup"><input id="video_root" type=text
      placeholder="/media/camera"><button
      onclick="browse('video_root')">Browse</button></div>
    <label class="f">Audio root &mdash; one folder per recorder</label>
    <div class="igroup"><input id="audio_root" type=text
      placeholder="/media/recorders"><button
      onclick="browse('audio_root')">Browse</button></div>
    <label class="f">Work dir</label>
    <div class="igroup"><input id="work_dir" type=text
      placeholder="./_wavesync"><button
      onclick="browse('work_dir')">Browse</button></div>
    <label class="f">Config file</label>
    <input id="cfg_path" type=text value="wavesync.config.json">
    <div class="btnrow">
      <button class="acc" onclick="autodetect()">Auto&#8209;detect</button>
      <button onclick="loadCfg()">Load</button>
      <button onclick="saveCfg()">Save</button></div>
    <div id="cfgmsg" class="msg info"></div>
  </div></div>

  <div class="panel"><header>Recorders / mics</header><div class="body">
    <table id="mics"><thead><tr>
      <th>name</th><th>folder</th><th style="width:44px">lane</th>
      <th style="width:42px">roll</th><th style="width:24px"></th>
    </tr></thead><tbody></tbody></table>
    <div class="btnrow"><button class="ghost"
      onclick="addMic()">+ add mic</button></div>
    <label class="f">Analysis filter (ffmpeg -af)</label>
    <input id="analysis_filter" type=text value="dynaudnorm=f=250:g=15:m=25">
    <div style="display:flex;gap:8px">
      <div style="flex:1"><label class="f">Sample rate</label>
        <input id="sample_rate" type=number value="8000"></div>
      <div style="flex:1"><label class="f">Min r (timeline)</label>
        <input id="min_r" type=text value="0.20"></div></div>
    <label class="f">Skip dirs</label>
    <input id="skip_dirs" type=text>
    <label class="f">Skip prefixes</label>
    <input id="skip_prefixes" type=text>
  </div></div>

  <div class="panel"><header>Scan</header><div class="body">
    <label class="f">Day folder</label>
    <div id="daylist"><div class="dempty">no video root set</div></div>
    <div class="cbxrow">
      <label class="cbx"><input type=checkbox id="alldays"><span
        class="box"></span>all days</label>
      <label class="cbx"><input type=checkbox id="timeline_only"><span
        class="box"></span>timeline only</label>
      <label class="cbx"><input type=checkbox id="preview"><span
        class="box"></span>previews</label></div>
    <label class="f">Video range (e.g. C0357-C0380)</label>
    <input id="video_range" type=text>
    <div class="btnrow">
      <button class="acc" id="runbtn" onclick="scan()">Run scan</button>
      <button onclick="selftest()">Self&#8209;test</button>
      <button onclick="outputs()">Outputs</button></div>
    <div id="scanmsg" class="msg info"></div>
  </div></div>
</div>

<div id="right">
  <div id="logwrap" class="panel" style="margin:0;border-left:none;
    border-top:none;display:flex;flex-direction:column">
    <header>Console
      <span class="toolbar"><button class="ghost"
        onclick="clearLog()">clear</button></span></header>
    <div id="log"><div><span class="t">[--:--:--]</span> wavesync ready.
      Set roots, Auto-detect, Run scan.</div></div>
  </div>
  <div id="repwrap" class="panel" style="margin:0;border-left:none;
    border-bottom:none;display:flex;flex-direction:column;flex:1">
    <header>Matches <span id="repday"
      style="letter-spacing:.02em;text-transform:none"></span>
      <span class="toolbar">
        <button onclick="loadReport()">Load report</button>
        <button onclick="saveBlocks()">Save blocks</button>
        <button onclick="scan(true)">Rebuild timeline</button>
      </span></header>
    <div id="rep-scroll"><table id="rep"><thead><tr>
      <th>audio file</th><th>mic</th><th class="num">shift s</th>
      <th class="num">r</th><th>covers</th><th>state</th>
      <th style="width:40px">block</th></tr></thead>
      <tbody><tr><td colspan=7 class="empty">
        Run a scan, then load the report for the day.</td></tr></tbody>
    </table></div>
  </div>
</div>
</div>

<div id="statusbar">
  <span class="st" id="stmsg">ready</span>
  <span class="right"><span id="stcfg"></span>
    <span>127.0.0.1 &middot; local only</span></span>
</div>

<div class="modal" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="box">
    <div class="hd" id="mpath"></div>
    <div class="ls" id="mlist"></div>
    <div class="ft"><button onclick="closeModal()">Cancel</button>
      <button class="acc" onclick="pickHere()">Select this folder</button>
    </div>
  </div>
</div>

<script>
let CFG=null, JOB=null, JOBN=0, BLOCKS={}, CURDAY="", LANECOL={};
const $=id=>document.getElementById(id);
const LC=["#c05555","#3d9b94","#4a6a8a","#8a6a3d","#7a5a9a","#4d7a3d"];

async function api(path,body){
  const opt=body?{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}:{};
  const r=await fetch(path,opt);return r.json();}
function msg(el,txt,cls){$(el).textContent=txt;$(el).className='msg '+(cls||'info');}
function stamp(){const d=new Date();return '['+
  String(d.getHours()).padStart(2,'0')+':'+
  String(d.getMinutes()).padStart(2,'0')+':'+
  String(d.getSeconds()).padStart(2,'0')+']';}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function log(line,cls){
  const l=$('log'),d=document.createElement('div');
  d.innerHTML='<span class="t">'+stamp()+'</span> '+
    '<span class="'+(cls||'')+'">'+esc(line)+'</span>';
  l.appendChild(d);l.scrollTop=l.scrollHeight;}
function clearLog(){$('log').innerHTML='';}
function status(t){$('stmsg').textContent=t;}

/* ---- persistence ---- */
const PKEY='wavesync.ui';
function persist(){const o={};
  ['video_root','audio_root','work_dir','cfg_path','analysis_filter',
   'sample_rate','min_r','skip_dirs','skip_prefixes']
   .forEach(id=>o[id]=$(id).value);
  localStorage.setItem(PKEY,JSON.stringify(o));}
function restore(){try{const o=JSON.parse(localStorage.getItem(PKEY)||'{}');
  Object.entries(o).forEach(([k,v])=>{if($(k))$(k).value=v;});
  }catch(e){}}
['video_root','audio_root','work_dir','cfg_path'].forEach(id=>{});
setInterval(()=>{if(CFG)persist()},2000);

/* ---- folder picker ---- */
let BTARGET=null;
async function browse(t){BTARGET=t;openModal($(t).value||'');}
async function openModal(path){
  const r=await api('/api/browse?path='+encodeURIComponent(path));
  $('modal').classList.add('open');
  if(r.error){$('mlist').innerHTML='<div class="dempty">'+esc(r.error)
    +'</div>';$('mpath').textContent=path||'/';return;}
  $('mpath').textContent=r.path||'(drives)';
  let h='';
  if(r.parent&&r.parent!==r.path)
    h+='<div class="drow" onclick="openModal(\''+jsq(r.parent)+'\')">'+
       '<span class="n">&#129048; up</span></div>';
  for(const d of r.dirs)
    h+='<div class="drow" onclick="openModal(\''+jsq(d.path)+'\')">'+
       '<span class="n">'+esc(d.name)+'/</span>'+
       (d.media?'<span class="c">'+d.media+'</span>':'')+'</div>';
  if(!r.dirs.length)h+='<div class="dempty">no subfolders</div>';
  $('mlist').innerHTML=h;$('mlist').scrollTop=0;}
function jsq(s){return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
function pickHere(){if(BTARGET){$(BTARGET).value=$('mpath').textContent;
  persist();}closeModal();refreshDays();}
function closeModal(){$('modal').classList.remove('open');}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')closeModal();});

/* ---- config ---- */
function collectCfg(){
  const mics={};
  document.querySelectorAll('#mics tbody tr').forEach(tr=>{
    const t=tr.querySelectorAll('input');
    const n=t[0].value.trim();if(!n)return;
    mics[n]={folder:t[1].value.trim()||n,
      lane:parseInt(t[2].value)||-1,rolling:t[3].checked};});
  return {video_root:$('video_root').value.trim(),
    audio_root:$('audio_root').value.trim(),
    work_dir:$('work_dir').value.trim()||'./_wavesync',
    mics:mics,
    skip_dirs:csv('skip_dirs'),skip_prefixes:csv('skip_prefixes'),
    analysis_filter:$('analysis_filter').value.trim(),
    sample_rate:parseInt($('sample_rate').value)||8000,
    min_r_timeline:parseFloat($('min_r').value)||0.2};}
function csv(id){return $(id).value.split(',').map(s=>s.trim())
  .filter(Boolean);}
function fillCfg(c){
  $('video_root').value=c.video_root||'';
  $('audio_root').value=c.audio_root||'';
  $('work_dir').value=c.work_dir||'';
  $('analysis_filter').value=c.analysis_filter||'dynaudnorm=f=250:g=15:m=25';
  $('sample_rate').value=c.sample_rate||8000;
  $('min_r').value=c.min_r_timeline??0.2;
  $('skip_dirs').value=(c.skip_dirs||[]).join(', ');
  $('skip_prefixes').value=(c.skip_prefixes||[]).join(', ');
  document.querySelector('#mics tbody').innerHTML='';
  Object.entries(c.mics||{}).forEach(([n,m])=>addMic(n,m));
  $('stcfg').textContent=$('cfg_path').value;
  refreshDays();}
function laneColor(lane){if(!(lane in LANECOL))
  LANECOL[lane]=LC[(-lane-1)%LC.length];return LANECOL[lane];}
function addMic(n,m){n=n||'';m=m||{};
  const tr=document.createElement('tr');
  const lane=m.lane??-1;
  tr.innerHTML='<td><input type=text value="'+esc(n)+'"></td>'+
   '<td><input type=text value="'+esc(m.folder||n)+'"></td>'+
   '<td><span class="lanechip" style="background:'+laneColor(lane)+
   '"></span><input type=number value="'+lane+
   '" style="width:38px;display:inline-block" oninput="'+
   "this.previousElementSibling.style.background=laneColor(this.value)"+
   '"></td>'+
   '<td><label class="cbx" style="padding:0"><input type=checkbox '+
   (m.rolling?'checked':'')+'><span class="box"></span></label></td>'+
   '<td><button class="xbtn" onclick="this.closest(\'tr\').remove()">'+
   '&times;</button></td>';
  document.querySelector('#mics tbody').appendChild(tr);}

async function autodetect(){
  const v=$('video_root').value.trim(),a=$('audio_root').value.trim();
  if(!v||!a){msg('cfgmsg','set video + audio roots first','err');return;}
  msg('cfgmsg','detecting…');
  const r=await api('/api/autodetect',{video_root:v,audio_root:a,
    work_dir:$('work_dir').value.trim()});
  if(r.error){msg('cfgmsg',r.error,'err');return;}
  CFG=r.config;fillCfg(CFG);persist();
  const n=Object.keys(CFG.mics).length;
  msg('cfgmsg','detected '+n+' mic'+(n===1?'':'s'),'ok');
  log('autodetect: '+n+' mic(s): '+Object.keys(CFG.mics).join(', '));}
async function loadCfg(){
  const r=await api('/api/config?path='+encodeURIComponent(
    $('cfg_path').value));
  if(r.config){CFG=r.config;fillCfg(CFG);
    msg('cfgmsg','loaded '+$('cfg_path').value,'ok');}
  else msg('cfgmsg',r.error||'no config yet','err');}
async function saveCfg(){
  CFG=collectCfg();
  const r=await api('/api/save_config',{path:$('cfg_path').value,
    config:CFG});persist();
  msg('cfgmsg',r.error||('saved '+r.saved),r.error?'err':'ok');}

/* ---- days ---- */
async function refreshDays(){
  const r=await api('/api/days?root='+encodeURIComponent(
    $('video_root').value.trim()));
  const el=$('daylist');el.innerHTML='';CURDAY='';
  const add=(val,label,count)=>{
    const d=document.createElement('div');
    d.className='drow';d.dataset.v=val;
    d.innerHTML='<span class="n">'+esc(label)+'</span>'+
      (count!=null?'<span class="c">'+count+' clips</span>':'');
    d.onclick=()=>{document.querySelectorAll('.drow.sel')
      .forEach(x=>x.classList.remove('sel'));
      d.classList.add('sel');CURDAY=val;};
    el.appendChild(d);};
  (r.days||[]).forEach(d=>add(d,d,r.counts[d]));
  if(r.flat)add(r.flat_day,r.flat_day+'  (flat)',null);
  if(!(r.days||[]).length&&!r.flat)
    el.innerHTML='<div class="dempty">no day folders found</div>';
  if(el.children.length){el.children[0].classList.add('sel');
    CURDAY=el.children[0].dataset.v;}}

/* ---- jobs ---- */
function setRunning(on){
  $('runbtn').disabled=on;
  $('jobdot').className='dot '+(on?'run':'');
  $('jobstate').textContent=on?'running':'idle';}
async function scan(tlOnly){
  if(JOB&&!JOB.done){log('a job is already running','l-acc');return;}
  CFG=collectCfg();persist();
  const all=$('alldays').checked;
  const day=all?'all':(CURDAY||'all');
  const body={config_path:$('cfg_path').value,config:CFG,day:day,
    timeline_only:tlOnly||$('timeline_only').checked,
    preview:$('preview').checked,video_range:$('video_range').value.trim()};
  msg('scanmsg','starting…');
  const r=await api('/api/scan',body);
  if(r.error){msg('scanmsg',r.error,'err');return;}
  JOB={id:r.job,done:false,day:day};JOBN=0;clearLog();
  log('$ '+r.cmd,'l-acc');setRunning(true);
  status('job '+r.job+' running');poll();}
async function selftest(){
  const r=await api('/api/scan',{selftest:true});
  JOB={id:r.job,done:false,day:null};JOBN=0;clearLog();
  log('$ '+r.cmd,'l-acc');setRunning(true);poll();}
async function poll(){
  if(!JOB)return;
  const r=await api('/api/job?id='+JOB.id+'&since='+JOBN);
  (r.lines||[]).forEach(l=>log(l));JOBN=r.n;
  JOB.done=r.done;
  if(!r.done){setTimeout(poll,600);return;}
  setRunning(false);
  const ok=r.code===0;
  $('jobdot').className='dot '+(ok?'ok':'err');
  $('jobstate').textContent=ok?'done':'exit '+r.code;
  status('job '+JOB.id+' '+(ok?'finished':'failed, exit '+r.code));
  msg('scanmsg',ok?'done':'failed (exit '+r.code+')',ok?'ok':'err');
  if(ok&&JOB.day&&JOB.day!=='all'&&!$('timeline_only').checked)
    loadReport(JOB.day);}

/* ---- report / blocks ---- */
async function loadReport(day){
  const d=day||CURDAY;
  if(!d){msg('scanmsg','pick a day first','err');return;}
  const r=await api('/api/report?config='+encodeURIComponent(
    $('cfg_path').value)+'&day='+encodeURIComponent(d));
  if(r.error){msg('scanmsg',r.error,'err');return;}
  CURDAY=d;$('repday').textContent='· '+d;BLOCKS=r.blocks||{};
  const tb=document.querySelector('#rep tbody');tb.innerHTML='';
  let nsel=0,ntot=0;
  for(const row of r.rows){
    const vb=BLOCKS[row.video]||[];
    const hd=document.createElement('tr');hd.className='vhead';
    hd.innerHTML='<td colspan=7>'+esc(row.video)+
      '<span class="vd">'+row.dur.toFixed(1)+'s</span></td>';
    tb.appendChild(hd);
    if(!row.matches.length){
      const tr=document.createElement('tr');tr.className='mrow';
      tr.innerHTML='<td class="name" colspan=5>'+
        '<span class="empty">no match</span></td>'+
        '<td><span class="pill">cam only</span></td>'+
        '<td>'+cbxHtml(row.video,'*',vb.includes('*'))+'</td>';
      tb.appendChild(tr);continue;}
    for(const m of row.matches){
      ntot++;if(m.selected)nsel++;
      const base=(m.wav_name||'').replace(/ \[(CONT|GUESS)\]$/,'');
      const blocked=vb.includes(base);
      const st=m.selected&&!blocked
        ?(m.nominated==='continuity'
          ?'<span class="pill review">CONT</span>'
          :'<span class="pill sel">selected</span>')
        :'<span class="pill blocked">'+(m.nominated||'rejected')+'</span>';
      const cov=(m.cov_a!=null)
        ?Math.round(m.cov_a)+'&ndash;'+Math.round(m.cov_b)+'s':'';
      const tr=document.createElement('tr');tr.className='mrow';
      tr.innerHTML='<td class="name">'+esc(m.wav_name||'')+'</td>'+
        '<td><span class="lanechip" style="background:'+
          laneColor(((CFG.mics||{})[m.mic]||{}).lane??-1)+'"></span>'+
          esc(m.mic||'')+'</td>'+
        '<td class="num">'+(m.shift_sec!=null?m.shift_sec.toFixed(2):'')+
        '</td>'+
        '<td class="num">'+(m.r_med!=null?m.r_med.toFixed(2):'')+'</td>'+
        '<td class="num" style="text-align:left;color:var(--dim)">'+cov+
        '</td>'+
        '<td>'+st+(m.time_ok===false?
          '<span class="tag-time">TIME</span>':'')+'</td>'+
        '<td>'+cbxHtml(row.video,m.wav_name,blocked)+'</td>';
      tb.appendChild(tr);}}
  status('report '+d+': '+nsel+'/'+ntot+' matches selected');}
function cbxHtml(v,w,on){
  return '<label class="cbx" style="padding:0"><input type=checkbox data-v="'+
    esc(v)+'" data-w="'+esc(w)+'"'+(on?' checked':'')+
    '><span class="box"></span></label>';}
async function saveBlocks(){
  const d=$('repday').textContent.replace('· ','').trim();
  if(!d){msg('scanmsg','load a report first','err');return;}
  const blocks={};
  document.querySelectorAll('#rep input[type=checkbox]:checked')
    .forEach(cb=>{(blocks[cb.dataset.v]=blocks[cb.dataset.v]||[])
      .push(cb.dataset.w);});
  const r=await api('/api/blocks',{config:$('cfg_path').value,day:d,
    blocks:blocks});
  msg('scanmsg',r.error||'blocks saved - rebuild to apply',
    r.error?'err':'ok');
  log('blocks saved for '+d);}
async function outputs(){
  const r=await api('/api/outputs?config='+encodeURIComponent(
    $('cfg_path').value));
  log('work_dir: '+r.work_dir);
  (r.timelines||[]).forEach(t=>log('  timeline  '+t));
  (r.reports||[]).forEach(t=>log('  report    '+t));}

$('video_root').addEventListener('change',refreshDays);
restore();loadCfg();
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
