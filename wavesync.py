#!/usr/bin/env python3
"""
wavesync.py - Align camera clips (with onboard audio) to external
recorder WAVs via onset-envelope cross-correlation, and emit an
FCPXML timeline importable into DaVinci Resolve (and other NLEs that
read Final Cut Pro XML).

Folder layout (all configurable -- nothing is hardcoded):

  <video_root>/<DAY>/*.MP4            one folder per shooting day
  <audio_root>/<mic>/<DAY>/*.wav      one folder per recorder per day

Outputs (under <work_dir>):
  reports/<DAY>_sync.json    every verified candidate + selection state
  reports/<DAY>_sync.csv     flat table for spreadsheets
  reports/<DAY>_blocks.json  YOUR corrections: wavs to keep off a clip
  timelines/<DAY>.fcpxml     the importable timeline
  previews/                  rendered sanity-check clips (--preview)
  cache/                     decoded-audio cache (safe to delete)

Offset convention:
  video_time_sec = wav_time_sec - shift_sec
  i.e. the sound at video t=0 lives at position `shift_sec` inside the wav.
  shift_sec < 0 means the wav starts AFTER the video started.
"""

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from scipy.signal import fftconvolve
from scipy.ndimage import median_filter

# ---------------------------------------------------------------------------
# configuration -- everything project-specific comes from wavesync.config.json
# (see wavesync.config.example.json). The values below are fallbacks.
# ---------------------------------------------------------------------------
SR = 8000           # decode rate for analysis (8k is plenty for Pearson)
FRAME = 1024        # analysis frame
HOP = 256           # hop
ENV_FPS = SR // HOP
F_LO, F_HI = 120, 6500   # Hz band for onset envelope
MIN_OVERLAP_S = 2.0      # ignore matches with less overlap
MIN_Z = 5.0              # min robust z-score (envelope stage; lenient --
                         # Pearson stage does the real filtering)
MIN_Z_RAW = 15.0         # min robust z-score (raw-waveform verify stage)
MIN_R_TIMELINE = 0.20    # weaker matches stay in report but off timeline

VI_DIR = SE_DIR = CACHE = REPORTS = TIMELINES = PREVIEWS = None
MICS = []           # ordered mic names (match lanes below)
LANES = {}          # mic name -> home lane on the timeline
MIC_FOLDERS = {}    # mic name -> folder inside audio_root (default: name)
DAY_MAP = {}        # mic name -> {video_day_folder: audio_day_folder}
ROLLING_MICS = set()  # mics left rolling through stretches -> [CONT] fills
SKIP_DIRS = {"main", "poznamky", "weird", "covered"}
SKIP_PREFIXES = ("final-test", "recording-day")
# analysis-only loudness boost: lifts quiet speech relative to noise so
# envelope + Pearson probes can lock onto low-level content (files untouched)
ANALYSIS_AF = "dynaudnorm=f=250:g=15:m=25"

DEFAULT_CONFIG_NAME = "wavesync.config.json"


def apply_config(cfg: dict, base: Path):
    """Populate module globals from the loaded config file."""
    global SR, ENV_FPS, MIN_OVERLAP_S, MIN_Z, MIN_Z_RAW, MIN_R_TIMELINE
    global VI_DIR, SE_DIR, CACHE, REPORTS, TIMELINES, PREVIEWS
    global MICS, LANES, MIC_FOLDERS, DAY_MAP
    global SKIP_DIRS, SKIP_PREFIXES, ANALYSIS_AF, ROLLING_MICS

    def _p(key, default=None):
        v = cfg.get(key, default)
        return None if v is None else Path(v).expanduser()

    VI_DIR = _p("video_root") or base / "videos"
    SE_DIR = _p("audio_root") or base / "audio"
    work = _p("work_dir") or base / "_wavesync"
    CACHE = work / "cache"
    REPORTS = work / "reports"
    TIMELINES = work / "timelines"
    PREVIEWS = work / "previews"

    mics = cfg.get("mics")
    if not mics:
        # fallback: every folder under audio_root is a mic, lanes -1..-n
        if SE_DIR.is_dir():
            mics = {d.name: {} for d in sorted(SE_DIR.iterdir())
                    if d.is_dir()}
        else:
            mics = {}
    MICS = list(mics.keys())
    ROLLING_MICS = set()
    for i, (name, mc) in enumerate(mics.items()):
        LANES[name] = int(mc.get("lane", -(i + 1)))
        MIC_FOLDERS[name] = mc.get("folder", name)
        if mc.get("rolling"):
            ROLLING_MICS.add(name)
    DAY_MAP = {k: v for k, v in cfg.get("day_map", {}).items()}

    SKIP_DIRS = set(cfg.get("skip_dirs", sorted(SKIP_DIRS)))
    SKIP_PREFIXES = tuple(cfg.get("skip_prefixes", list(SKIP_PREFIXES)))
    ANALYSIS_AF = cfg.get("analysis_filter", ANALYSIS_AF)
    SR = int(cfg.get("sample_rate", SR))
    ENV_FPS = SR // HOP
    MIN_OVERLAP_S = float(cfg.get("min_overlap_s", MIN_OVERLAP_S))
    MIN_Z = float(cfg.get("min_z", MIN_Z))
    MIN_Z_RAW = float(cfg.get("min_z_raw", MIN_Z_RAW))
    MIN_R_TIMELINE = float(cfg.get("min_r_timeline", MIN_R_TIMELINE))


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    apply_config(cfg, path.resolve().parent)
    return cfg


def decode_audio(path: Path, sr: int = None) -> np.ndarray:
    """Decode a media file's first audio stream to mono f32."""
    sr = sr or SR
    """Decode a media file's first audio stream to mono f32 @ sr."""
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-vn", "-ac", "1", "-af", ANALYSIS_AF, "-ar", str(sr),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(
            f"ffmpeg decode failed for {path.name}: "
            f"{p.stderr.decode(errors='replace')[:300]}")
    return np.frombuffer(p.stdout, dtype=np.float32)


def ffprobe_field(path: Path, entry: str) -> str:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entry,
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return p.stdout.strip()


def onset_envelope(x: np.ndarray) -> np.ndarray:
    """Spectral-flux onset envelope; robust to mic EQ/gain differences."""
    n_frames = max(1, (len(x) - FRAME) // HOP + 1)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(FRAME).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(FRAME, 1 / SR)
    band = (freqs >= F_LO) & (freqs <= F_HI)
    logm = np.log1p(mag[:, band] * 1000)
    flux = np.diff(logm, axis=0, prepend=logm[:1])
    flux[flux < 0] = 0
    env = flux.sum(axis=1)
    med = median_filter(env, size=ENV_FPS | 1)
    env = np.clip(env - med, 0, None)
    std = env.std()
    return (env / std).astype(np.float32) if std > 0 else env * 0


def env_cache_path(path: Path) -> Path:
    st = path.stat()
    key = f"{path}|{st.st_size}|{st.st_mtime_ns}|SR{SR}|{ANALYSIS_AF}"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE / f"{h}.npy"


def raw_cache_path(path: Path) -> Path:
    st = path.stat()
    key = f"raw|{path}|{st.st_size}|{st.st_mtime_ns}|SR{SR}|{ANALYSIS_AF}"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE / "raw" / f"{h}.npy"


def get_raw(path: Path) -> np.ndarray:
    """Disk-cached full mono decode at SR — wavs decode once ever."""
    cp = raw_cache_path(path)
    if cp.exists():
        return np.load(cp)
    a = decode_audio_chunked(path)
    cp.parent.mkdir(parents=True, exist_ok=True)
    np.save(cp, a)
    return a


def get_env(path: Path) -> np.ndarray:
    cp = env_cache_path(path)
    if cp.exists():
        return np.load(cp)
    env = onset_envelope(decode_audio(path))
    cp.parent.mkdir(parents=True, exist_ok=True)
    np.save(cp, env)
    return env


def correlate(env_v: np.ndarray, env_w: np.ndarray):
    """
    Returns (shift_frames, peak, z, overlap_sec).
    shift_frames: env index inside wav where video t=0 lands (may be <0).
    """
    v = env_v - env_v.mean()
    w = env_w - env_w.mean()
    if v.std() == 0 or w.std() == 0 or len(v) < 10 or len(w) < 10:
        return None
    corr = fftconvolve(v, w[::-1], mode="full")
    imax = int(np.argmax(corr))
    # v[i] ~ w[i + shift]; conv peak at imax gives shift = len(w)-1 - imax
    lag = (len(w) - 1) - imax
    peak = float(corr[imax])
    bg = np.delete(corr, slice(max(0, imax - ENV_FPS),
                             min(len(corr), imax + ENV_FPS)))
    med = np.median(bg)
    mad = np.median(np.abs(bg - med)) * 1.4826
    z = (peak - med) / mad if mad > 0 else 0.0
    ov_start = max(0, lag)
    ov_end = min(len(v) + lag, len(w))
    overlap = max(0, ov_end - ov_start) / ENV_FPS
    return lag, peak, float(z), overlap


def gather_wavs(day: str, se_dir: Path):
    out = []
    for mic in MICS:
        sub = DAY_MAP.get(mic, {}).get(day, day)
        d = se_dir / MIC_FOLDERS.get(mic, mic) / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.wav")):
            rel = p.relative_to(d)
            if any(part.lower() in SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if p.name.lower().startswith(SKIP_PREFIXES):
                continue
            out.append((mic, p))
    return out


def run_day(day: str, topn: int = 2, vi_dir: Path = None,
            se_dir: Path = None, only_video: str = None,
            video_range: tuple = None):
    vi_dir = vi_dir or VI_DIR
    se_dir = se_dir or SE_DIR
    vdir = vi_dir / day
    videos = sorted(set(vdir.glob("*.MP4")) | set(vdir.glob("*.mp4")))
    if only_video:
        videos = [v for v in videos if v.name == only_video]
    if video_range:
        lo, hi = video_range
        videos = [v for v in videos if lo <= v.stem <= hi]
    wavs = gather_wavs(day, se_dir)
    print(f"[{day}] {len(videos)} videos x {len(wavs)} wavs", flush=True)

    wav_envs = {}
    for mic, wp in wavs:
        try:
            wav_envs[wp] = get_env(wp)
        except Exception as e:
            print(f"  !! cannot decode {wp.name}: {e}", flush=True)
    wav_dur = {}
    for mic, wp in wavs:
        if wp in wav_envs:
            wav_dur[wp] = float(ffprobe_field(wp, "format=duration") or 0)
    print(f"  decoded {len(wav_envs)} wav envelopes", flush=True)

    wav_raw = {}   # path -> full 16k mono decode (reused across candidates)

    results = []
    for vi, vp in enumerate(videos):
        try:
            env_v = get_env(vp)
            vdur = float(ffprobe_field(vp, "format=duration"))
            v_ctime = ffprobe_field(vp, "format_tags=creation_time")
            v_audio = get_raw(vp)
        except Exception as e:
            print(f"  !! cannot process {vp.name}: {e}", flush=True)
            continue
        cands = []
        for mic, wp in wavs:
            ew = wav_envs.get(wp)
            if ew is None:
                continue
            r = correlate(env_v, ew)
            if r is None:
                continue
            lag, peak, z, ov = r
            cands.append({
                "mic": mic, "wav": str(wp), "wav_name": wp.name,
                "wav_dur": wav_dur.get(wp, 0.0),
                "shift_sec": lag / ENV_FPS, "peak": peak,
                "z": z, "overlap_sec": ov,
            })
        cands.sort(key=lambda c: -c["z"])
        keep = [c for c in cands
                if c["z"] >= MIN_Z and c["overlap_sec"] >= MIN_OVERLAP_S]
        # time-overlap nomination: wavs whose recording window intersects
        # the video's get probed even with weak envelopes (short toggled
        # segments, clapperboard-only clips)
        for mic, wp in wavs:
            if time_consistent(v_ctime, vdur, wp,
                               wav_dur.get(wp, 0.0)) is not True:
                continue
            ex = next((c for c in keep if c["wav"] == str(wp)), None)
            if ex is not None:
                ex["nominated"] = "time"   # upgrade -> priority probing
            else:
                keep.append({
                    "mic": mic, "wav": str(wp), "wav_name": wp.name,
                    "wav_dur": wav_dur.get(wp, 0.0),
                    "shift_sec": 0.0, "peak": 0.0, "z": 0.0,
                    "overlap_sec": 0.0, "nominated": "time",
                })
        # raw-verify candidates; a video may legitimately match several wavs
        # per mic (recorder toggled mid-take) -> keep ALL verified matches
        TOPK = 10  # per mic, envelope-ranked
        matches = []
        for mic in MICS:
            pool = [c for c in keep if c["mic"] == mic]
            pool.sort(key=lambda c: (c.get("nominated") != "time",
                                     -c["z"]))
            for c in pool[:TOPK]:
                wp = Path(c["wav"])
                if wp not in wav_raw:
                    wav_raw[wp] = get_raw(wp)
                loc = locate_pair(v_audio, wav_raw[wp])
                if loc is None:
                    c["verified"] = False
                    continue
                c.update({
                    "verified": True,
                    "shift_sec": loc["shift_sec"],
                    "r_med": loc["r_med"],
                    "n_support": loc["n_support"],
                    "cov_a": loc["cov_a"], "cov_b": loc["cov_b"],
                    "time_ok": time_consistent(v_ctime, vdur, wp,
                                               c["wav_dur"]),
                })
                if "drift_ppm" in loc:
                    c["drift_ppm"] = loc["drift_ppm"]
                    c["drift_ms"] = loc["drift_ms"]
                matches.append(c)
        # dedup: same wav may surface twice via identical-content takes
        seen = {}
        for c in matches:
            k = (c["mic"], c["wav"])
            if k not in seen or c["r_med"] > seen[k]["r_med"]:
                seen[k] = c
        matches = sorted(seen.values(), key=lambda c: (c["mic"], c["shift_sec"]))
        matches = select_coverage(matches, vdur)
        results.append({
            "video": str(vp), "video_name": vp.name, "video_dur": vdur,
            "creation_time": v_ctime,
            "candidates": cands[:topn * len(MICS)],
            "matches": matches,
        })
        tag = "  ".join(
            f"{c['mic']}:{c['wav_name']} @{c['shift_sec']:+.2f}s "
            f"r={c.get('r_med', 0):.2f} "
            f"cov=[{c.get('cov_a',0):.0f}-{c.get('cov_b',0):.0f}]"
            + ("" if c.get("selected") else " [dup/alt]")
            + (f" drift={c['drift_ms']:.0f}ms" if "drift_ms" in c else "")
            + ("" if c.get("time_ok") in (None, True) else " TIME-MISMATCH")
            for c in matches)
        print(f"  [{vi+1}/{len(videos)}] {vp.name}  {tag or 'NO MATCH'}",
              flush=True)

    REPORTS.mkdir(exist_ok=True)
    if only_video or video_range:
        # merge into the existing day report instead of overwriting it;
        # manually injected / continuity matches survive rescans
        jp = REPORTS / f"{day}_sync.json"
        if jp.exists():
            old = {r["video_name"]: r for r in json.loads(jp.read_text())
                   if str(vi_dir) in r["video"]}
            for r in results:
                prev = old.get(r["video_name"])
                if prev:
                    r["matches"].extend(
                        m for m in prev["matches"]
                        if m.get("nominated") in ("manual", "continuity"))
                old[r["video_name"]] = r
            results = sorted(old.values(),
                             key=lambda r: (r["creation_time"] or "",
                                            r["video_name"]))
            videos = [Path(r["video"]) for r in results]
    apply_user_blocks(results, day)
    enforce_file_consistency(results)
    fill_continuity(results)
    (REPORTS / f"{day}_sync.json").write_text(json.dumps(results, indent=1))
    cpath = REPORTS / f"{day}_sync.csv"
    with cpath.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video", "dur_s", "mic", "wav", "shift_sec",
                    "z_env", "r_med", "n_support", "on_timeline",
                    "drift_ms", "covers_video", "time_ok"])
        for r in results:
            if not r["matches"]:
                w.writerow([r["video_name"], f"{r['video_dur']:.2f}",
                            "", "", "", "", "", "", "", "", "", "", ""])
            for c in r["matches"]:
                w.writerow([r["video_name"], f"{r['video_dur']:.2f}",
                            c["mic"], c["wav_name"], f"{c['shift_sec']:.3f}",
                            f"{c['z']:.1f}", f"{c.get('r_med', 0):.3f}",
                            c.get("n_support", ""),
                            int(c.get("selected", False)),
                            f"{c.get('drift_ms', ''):}",
                            f"{c.get('cov_a', 0):.1f}-{c.get('cov_b', 0):.1f}",
                            c.get("time_ok", "")])
    fx = write_fcpxml(day, results, videos, [p for m, p in wavs],
                      TIMELINES / f"{day}.fcpxml")
    print(f"  -> reports/{cpath.name} + {day}_sync.json + "
          f"timelines/{fx.name}", flush=True)
    return results


def decode_segment(path: Path, start: float, dur: float,
                   sr: int = None) -> np.ndarray:
    sr = sr or SR
    cmd = [
        "ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
        "-i", str(path), "-vn", "-ac", "1", "-af", ANALYSIS_AF,
        "-ar", str(sr),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(p.stdout, dtype=np.float32)


def decode_audio_chunked(path: Path, sr: int = None,
                         chunk_s: float = 1800.0):
    """Decode whole file; yields float32 array (chunked decode internally)."""
    dur_s = float(ffprobe_field(path, "format=duration") or 0)
    if dur_s <= chunk_s:
        return decode_audio(path, sr)
    parts, t = [], 0.0
    while t < dur_s:
        parts.append(decode_segment(path, t, min(chunk_s, dur_s - t), sr))
        t += chunk_s
    return np.concatenate(parts)


def best_probe_start(env: np.ndarray, probe_s: float) -> float:
    """Video time (s) of the most energetic probe_s-second window."""
    win = max(1, int(probe_s * ENV_FPS))
    if len(env) <= win:
        return 0.0
    energy = np.convolve(env, np.ones(win, dtype=np.float32), mode="valid")
    return int(np.argmax(energy)) / ENV_FPS


def slide_pearson(probe: np.ndarray, w: np.ndarray):
    """
    Pearson r between `probe` and every len(probe)-window of w.
    Returns (wav_pos_sec, r at best |r|) or None.
    Note: r can be negative (one recorder inverts polarity) -> rank by |r|.
    """
    n = len(probe)
    if len(w) <= n or n < SR * 2:
        return None
    vv = (probe - probe.mean()).astype(np.float64)
    w64 = w.astype(np.float64)
    num = fftconvolve(w64, vv[::-1], mode="valid")    # Σ vv[i]*w[k+i]
    cs = np.concatenate(([0.0], np.cumsum(w64)))
    cs2 = np.concatenate(([0.0], np.cumsum(w64 * w64)))
    sw = cs[n:] - cs[:-n]
    sw2 = cs2[n:] - cs2[:-n]
    var_w = np.maximum(sw2 - sw * sw / n, 1e-12)
    r = num / (np.sqrt(var_w) * np.linalg.norm(vv) + 1e-12)
    k = int(np.argmax(np.abs(r)))
    return k / SR, float(r[k])


MIN_R = 0.12        # Pearson |r| for a probe to count as "same event"
MIN_R_STRONG = 0.25 # single-probe verification for very short clips


def _shift_clusters(hits):
    """Largest group of hits agreeing on a shift (+-0.4s).
    hit = (t_short, pos_in_long, |r|); shift = t_short - pos_in_long."""
    if not hits:
        return []
    best, grp_best = -1.0, []
    for h in hits:
        s = h[0] - h[1]
        grp = [hh for hh in hits if abs(hh[0] - hh[1] - s) <= 0.4]
        score = sum(g[2] for g in grp)
        if len(grp) > len(grp_best) or (len(grp) == len(grp_best)
                                        and score > best):
            grp_best, best = grp, score
    return grp_best


def energy_peaks(x: np.ndarray, probe_s: float, n_max: int = 14):
    """Probe start times at the strongest RMS peaks of x (50ms frames),
    min separation ~0.75*probe_s, only frames > 1.5x median energy."""
    frame = SR // 20
    nf = len(x) // frame
    if nf < 2:
        return [0.0]
    e = np.abs(x[:nf * frame]).reshape(nf, frame).astype(np.float64)
    rms = np.sqrt((e ** 2).mean(axis=1))
    med = np.median(rms)
    floor = max(med * 1.5, 1e-4)
    gap = int(probe_s * 0.75 * 20)
    taken = np.zeros(nf, dtype=bool)
    out = []
    for i in np.argsort(-rms):
        if len(out) >= n_max or rms[i] < floor:
            break
        if taken[max(0, i - gap):i + gap + 1].any():
            continue
        out.append(i / 20.0)
        taken[max(0, i - gap):i + gap + 1] = True
    dur = len(x) / SR
    out = [t for t in out if t <= dur - probe_s]
    if not out:
        out = [max(0.0, dur / 2 - probe_s / 2)]
    return sorted(out)


def locate_pair(v_audio: np.ndarray, w_audio: np.ndarray):
    """
    Find linear alignment between video audio and wav audio.
    Probes the SHORTER signal inside the longer one (wav->video when the
    wav is short: catches small mic-toggled segments inside long takes).
    Returns dict(shift_sec, r_med, n_support, cov_a, cov_b, drift) or None.
      shift_sec: wav time where video t=0 lands (s.t. video t -> wav t+shift)
      cov_a/cov_b: covered VIDEO interval
    """
    vd, wd = len(v_audio) / SR, len(w_audio) / SR
    if vd < 3.0 or wd < 3.0:
        return None
    probe_s = 4.0
    # probe the SHORTER signal at its energetic peaks (clapperboard hits,
    # speech) — quiet stretches don't correlate anyway
    if wd <= vd:
        probe_src, host_src = w_audio, v_audio
        wav_is_probe = True
    else:
        probe_src, host_src = v_audio, w_audio
        wav_is_probe = False
    starts = energy_peaks(probe_src, probe_s, n_max=14)
    hits = []
    for t in starts:
        i = int(t * SR)
        probe = probe_src[i:i + int(probe_s * SR)]
        if len(probe) < probe_s * SR * 0.9 or probe.std() < 1e-5:
            continue
        r = slide_pearson(probe, host_src)
        if r and r[1] is not None and abs(r[1]) >= MIN_R:
            hits.append((float(t), r[0], abs(r[1])))
    inl = _shift_clusters(hits)
    # phase 2: sweep inside the implied overlap to extend coverage
    if inl:
        if wav_is_probe:
            s0 = float(np.median([h[1] * -1.0 + h[0] for h in inl]))
            lo_w, hi_w = max(0.0, s0), min(wd, vd + s0)   # wav-side bounds
        else:
            s0 = float(np.median([h[1] - h[0] for h in inl]))
            lo_w, hi_w = max(0.0, -s0), min(vd, wd - s0)  # video-side
        span = hi_w - lo_w
        if span > probe_s:
            for t in np.linspace(lo_w, hi_w - probe_s,
                                 int(np.clip(span / 3, 2, 16))):
                if any(abs(t - h[0]) < probe_s / 2 for h in hits):
                    continue
                i = int(t * SR)
                probe = probe_src[i:i + int(probe_s * SR)]
                if len(probe) < probe_s * SR * 0.9 or probe.std() < 1e-5:
                    continue
                r = slide_pearson(probe, host_src)
                if r and r[1] is not None and abs(r[1]) >= MIN_R:
                    hits.append((float(t), r[0], abs(r[1])))
            inl = _shift_clusters(hits)
    if len(inl) < 2 and not (len(inl) == 1 and inl[0][2] >= 0.45):
        return None
    # shift: wav position of video t=0.
    # wav->video hit: wav t0 <-> video t=(pos); shift = w0 - pos
    # video->wav hit: video t0 <-> wav pos;   shift = pos - t0
    if wav_is_probe:
        shifts = np.array([h[1] * -1.0 + h[0] for h in inl])  # w0 - p
    else:
        shifts = np.array([h[1] - h[0] for h in inl])          # p - t0
    shift = float(np.median(shifts))
    r_med = float(np.median([h[2] for h in inl]))
    # video coverage: each hit covers host [pos, pos+probe_s]
    # -> map to video time (host coords are already video when probing wav)
    if wav_is_probe:
        cov_a = max(0.0, min(h[1] for h in inl) - 0.5)
        cov_b = min(vd, max(h[1] + probe_s for h in inl) + 0.5)
    else:
        cov_a = max(0.0, min(h[0] for h in inl) - 0.5)
        cov_b = min(vd, max(h[0] + probe_s for h in inl) + 0.5)
    # clip to what the wav can physically cover
    cov_a = max(cov_a, -shift)
    cov_b = min(cov_b, wd - shift)
    if cov_b - cov_a < 1.0:
        return None
    out = {"shift_sec": shift, "r_med": r_med, "n_support": len(inl),
           "cov_a": cov_a, "cov_b": cov_b}
    if len(inl) >= 3:
        if wav_is_probe:
            xs = np.array([h[0] for h in inl])   # wav t
            ys = np.array([h[1] for h in inl])   # video t
            slope = np.polyfit(xs, ys, 1)[0]
            drift = (1.0 / slope - 1.0) if abs(slope) > 1e-6 else 0.0
        else:
            xs = np.array([h[0] for h in inl])   # video t
            ys = np.array([h[1] for h in inl])   # wav t
            slope = np.polyfit(xs, ys, 1)[0]
            drift = slope - 1.0
        out["drift_ppm"] = drift * 1e6
        out["drift_ms"] = drift * (cov_b - cov_a) * 1000.0
    return out


def time_consistent(video_ctime_iso: str, vdur: float,
                    wav_path: Path, wav_dur: float):
    """
    Does the wav's recording window overlap the video's?
    Video: creation_time(UTC) .. +vdur.  Wav: mtime-wav_dur .. mtime (local).
    Returns True/False, or None if the recorder clock was unset
    (mtime before 2010 -> filenames/mtimes are garbage, e.g. DR-10L).
    """
    if not video_ctime_iso:
        return None
    try:
        v0 = datetime.fromisoformat(
            video_ctime_iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    v1 = v0.timestamp() + vdur
    w_end = wav_path.stat().st_mtime
    if w_end < 1262300400:      # before 2010-01-01 -> clock was unset
        return None
    # clock set but wrong by a huge margin (e.g. DR-10 stamped 2029 while
    # the video is 2026) -> untrustworthy; a genuine "recorded elsewhere"
    # differs by hours or days, not years
    if abs(w_end - v0.timestamp()) > 30 * 86400:
        return None
    w_start = w_end - wav_dur
    return (w_start < v1) and (w_end > v0.timestamp())


def media_info(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:format_tags=timecode,time_reference"
         ":stream=codec_type,channels,sample_rate,"
         "avg_frame_rate,width,height:stream_tags=timecode",
         "-of", "json", str(path)],
        capture_output=True, text=True)
    d = json.loads(out.stdout)
    info = {"dur": float(d["format"]["duration"]), "video": None,
            "channels": 0, "rate": 48000, "tc": 0.0}
    tags = d.get("format", {}).get("tags", {})
    for s in d.get("streams", []):
        if s["codec_type"] == "video" and info["video"] is None:
            fps = s.get("avg_frame_rate", "25/1")
            num, _, den = fps.partition("/")
            info["video"] = {"w": s.get("width", 1920),
                             "h": s.get("height", 1080),
                             "fps_num": int(num), "fps_den": int(den or 1)}
        elif s["codec_type"] == "audio" and not info["channels"]:
            info["channels"] = s.get("channels", 1)
            info["rate"] = int(s.get("sample_rate", 48000))
        stc = s.get("tags", {}).get("timecode")
        if stc:
            tags["timecode"] = stc   # XAVC keeps TC on a data stream
    # timecode domain: video TC tag "HH:MM:SS:FF" @fps, wav BWF
    # time_reference = samples since midnight
    tc = tags.get("timecode")
    if tc and info["video"]:
        try:
            h, m, s2, f = (int(x) for x in tc.split(":"))
            fps = info["video"]["fps_num"] / info["video"]["fps_den"]
            info["tc"] = h * 3600 + m * 60 + s2 + f / fps
        except (ValueError, ZeroDivisionError):
            pass
    elif tags.get("time_reference"):
        try:
            info["tc"] = float(tags["time_reference"]) / info["rate"]
        except (ValueError, ZeroDivisionError):
            pass
    return info


def file_uri(path: Path) -> str:
    """FCPXML asset URI; Resolve on Windows expects
    file://localhost/W%3A/path (encoded drive colon)."""
    from urllib.parse import quote
    return "file://localhost/" + quote(str(path).replace("\\", "/"),
                                       safe="/:")


def write_fcpxml(day: str, results: list, videos: list, wavs: list,
                 out: Path):
    """
    Resolve-importable timeline: videos sequential on the spine,
    verified mic segments on their configured home lanes clipped to the
    region they actually cover, camera audio stays embedded in V1.
    Audio-only wavs that overlap a clip's window park on lane -3.
    """
    lane_busy = {}  # lane -> [(start, end)]; clips never overlap on a lane

    def lane_free(lane, s, e):
        return all(e <= b0 + 0.02 or s >= b1 - 0.02
                   for b0, b1 in lane_busy.get(lane, ()))

    def pick_lane(home, s, e):
        # keep the true offset; on collision move DOWN to a free lane
        lane = home
        while not lane_free(lane, s, e):
            lane -= 1
        lane_busy.setdefault(lane, []).append((s, e))
        return lane

    infos = {}
    def info(p):
        if p not in infos:
            infos[p] = media_info(p)
        return infos[p]

    def ft(sec: float) -> str:
        # FCPXML times must be rational (e.g. "679200/1000s"), not decimal
        return f"{int(round(sec * 1000))}/1000s"

    # one <format> per distinct video geometry/fps (asset must match media)
    fmt_ids = {}
    fmts = []
    def fmt_for(p: Path):
        v = info(p)["video"] or {"w": 1920, "h": 1080,
                                 "fps_num": 25, "fps_den": 1}
        key = (v["w"], v["h"], v["fps_num"], v["fps_den"])
        if key not in fmt_ids:
            fid = f"r{len(fmt_ids)+1}"
            fmt_ids[key] = fid
            fmts.append(
                f'<format id="{fid}" '
                f'frameDuration="{v["fps_den"]}/{v["fps_num"]}s" '
                f'width="{v["w"]}" height="{v["h"]}"/>')
        return fmt_ids[key]

    for vp in videos:
        fmt_for(vp)
    seq_fmt = fmt_ids[next(iter(fmt_ids))]

    assets, items = [], []
    aid = {}
    def asset_id(p: Path, is_video: bool):
        if p in aid:
            return aid[p]
        aid[p] = f"a{len(aid)+2}"
        i = info(p)
        uid = hashlib.md5(str(p).encode()).hexdigest().upper()
        # Resolve exports put the URI on a media-rep child, not asset src
        if is_video:
            assets.append(
                f'<asset id="{aid[p]}" name="{escape(p.name)}" '
                f'uid="{uid}" start="{ft(i["tc"])}" '
                f'duration="{ft(i["dur"])}" hasVideo="1" '
                f'videoSources="1" format="{fmt_for(p)}" hasAudio="1" '
                f'audioSources="1" audioChannels="{max(1, i["channels"])}" '
                f'audioRate="{i["rate"]}">'
                f'<media-rep kind="original-media" src="{file_uri(p)}"/>'
                f'</asset>')
        else:
            assets.append(
                f'<asset id="{aid[p]}" name="{escape(p.name)}" '
                f'uid="{uid}" start="{ft(i["tc"])}" '
                f'duration="{ft(i["dur"])}" hasAudio="1" '
                f'audioSources="1" audioChannels="{i["channels"]}" '
                f'audioRate="{i["rate"]}">'
                f'<media-rep kind="original-media" src="{file_uri(p)}"/>'
                f'</asset>')
        return aid[p]

    # order videos chronologically (creation_time) then by name
    order = sorted(results, key=lambda r: (r["creation_time"] or "",
                                           r["video_name"]))
    pos = 0.0
    used_wavs = set()
    for r in order:
        vp = Path(r["video"])
        vid = asset_id(vp, True)
        vd = r["video_dur"]
        items.append(
            f'<asset-clip name="{escape(r["video_name"])}" ref="{vid}" '
            f'offset="{ft(pos)}" start="{ft(info(vp)["tc"])}" '
            f'duration="{ft(vd)}" '
            f'enabled="1" format="{fmt_for(vp)}" audioRole="dialogue" '
            f'tcFormat="NDF">'
            f'<adjust-transform anchor="0 0" scale="1 1" position="0 0"/>'
            f'</asset-clip>')
        for c in r["matches"]:
            if not c.get("selected"):
                continue
            cov_span = (c.get("cov_b") or 0) - (c.get("cov_a") or 0)
            if (c.get("r_med", 0) < MIN_R_TIMELINE
                    and not (c.get("time_ok") is not False
                             and cov_span >= 0.8 * vd)):
                continue
            wp = Path(c["wav"])
            used_wavs.add(str(wp))
            wid = asset_id(wp, False)
            s = c["shift_sec"]          # wav time where video t=0 lands
            # place the wav's whole physical overlap with the video on the
            # timeline (user trims in Resolve); probed cov_* is evidence only
            a, b = max(0.0, -s), min(vd, c["wav_dur"] - s)
            seg = b - a
            if seg <= 0.05:
                continue
            # video t=a sits at timeline pos+a and equals wav t=a+s
            in_pt = a + s
            off = pos + a
            items.append(
                f'<asset-clip name="{escape(c["wav_name"])}" ref="{wid}" '
                f'lane="{pick_lane(LANES[c["mic"]], off, off + seg)}" '
                f'offset="{ft(off)}" '
                f'start="{ft(info(wp)["tc"] + in_pt)}" '
                f'duration="{ft(seg)}" audioRole="dialogue"/>')
        pos += vd

    # audio-only wavs: park only wavs that overlap a timeline video's
    # recording window (orphans recorded during a take but not matching);
    # placed at their true relative time on lane -3. Everything else goes
    # to the event bin (imported, off the timeline).
    windows = []
    vid_pos = {}
    for r in order:
        ct = r.get("creation_time")
        if not ct:
            continue
        try:
            v0 = datetime.fromisoformat(
                ct.replace("Z", "+00:00")).timestamp()
            windows.append((v0, v0 + r["video_dur"], r))
        except ValueError:
            pass
    # timeline position of each video
    p_run = 0.0
    for r in order:
        vid_pos[id(r)] = p_run
        p_run += r["video_dur"]
    unmatched = [p for p in wavs if str(p) not in used_wavs]
    binned = []
    for p in unmatched:
        w_end = p.stat().st_mtime
        w_start = w_end - info(p)["dur"]
        hit = next((x for x in windows
                    if w_end >= 1262300400 and w_start < x[1]
                    and w_end > x[0]), None)
        if hit is None:
            binned.append(p)
            continue
        wid = asset_id(p, False)
        rel_off = max(0.0, vid_pos[id(hit[2])] + (w_start - hit[0]))
        w_dur = info(p)["dur"]
        # wavs the user blocked on a video must not park over that
        # video's timeline region even though they physically overlap it
        blk_hit = next(
            (r2 for r2 in order
             if p.name in r2.get("_blocked", [])
             and rel_off < vid_pos[id(r2)] + r2["video_dur"]
             and rel_off + w_dur > vid_pos[id(r2)]), None)
        if blk_hit is not None:
            binned.append(p)
            continue
        items.append(
            f'<asset-clip name="AUDIO-ONLY {escape(p.name)}" ref="{wid}" '
            f'lane="{pick_lane(-3, rel_off, rel_off + w_dur)}" '
            f'offset="{ft(rel_off)}" '
            f'start="{ft(info(p)["tc"])}" '
            f'duration="{ft(w_dur)}" audioRole="dialogue"/>')
        pos = max(pos, rel_off + w_dur)
    bin_items = []
    for p in binned:
        wid = asset_id(p, False)
        bin_items.append(
            f'<asset-clip name="{escape(p.name)}" ref="{wid}" '
            f'start="{ft(info(p)["tc"])}" duration="{ft(info(p)["dur"])}" '
            f'audioRole="dialogue"/>')

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.9">\n<resources>\n' + "\n".join(fmts) + "\n"
        + "\n".join(assets) +
        '\n</resources>\n<library><event name="sync-' + day + '">\n'
        + "\n".join(bin_items) +
        '\n<project name="sync-' + day + '">\n'
        f'<sequence format="{seq_fmt}" duration="{ft(pos)}" '
        'tcStart="0s" tcFormat="NDF" '
        'audioLayout="stereo" audioRate="48k">\n<spine>\n'
        + "\n".join(items) +
        '\n</spine>\n</sequence>\n</project>\n</event></library>\n'
        '</fcpxml>\n')
    TIMELINES.mkdir(exist_ok=True)
    out.write_text(xml, encoding="utf-8")
    return out


def _video_start(r):
    ct = r.get("creation_time")
    if not ct:
        return None
    try:
        return datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def apply_user_blocks(results, day):
    """User-verified rejections live in reports/{day}_blocks.json:
    {video_name: [wav_name, ...]} or ["*"] to block all ext audio.
    Blocked matches are deselected and never continuity-filled."""
    bp = REPORTS / f"{day}_blocks.json"
    if not bp.exists():
        return
    blocks = json.loads(bp.read_text(encoding="utf-8"))
    for r in results:
        vb = blocks.get(r["video_name"], [])
        if not vb:
            continue
        if "*" in vb:
            r["no_fill"] = True
        r["_blocked"] = vb
        for m in r["matches"]:
            base = m["wav_name"].replace(" [CONT]", "").replace(" [GUESS]", "")
            if "*" in vb or base in vb:
                m["selected"] = False


def enforce_file_consistency(results):
    """A wav file has exactly ONE real recording window. For each file,
    real matches all imply the same wstart = video_start - shift.
    Selected matches whose implied wstart deviates >10s from the
    file's dominant wstart are playback echoes -> deselect."""
    from collections import defaultdict
    byf = defaultdict(list)
    for r in results:
        t0 = _video_start(r)
        if t0 is None:
            continue
        for m in r["matches"]:
            if m.get("selected") and m.get("nominated") not in ("manual",):
                byf[(m["mic"], m["wav"])].append((t0, m))
    for (_mic, _w), lst in byf.items():
        if len(lst) < 2:
            continue
        # cluster implied wstarts within 10s; weight = cov_span * r
        items = []
        for t0, m in lst:
            span = max(1.0, (m.get("cov_b") or 0) - (m.get("cov_a") or 0))
            items.append((t0 - m["shift_sec"], span * max(m.get("r_med", 0.05), 0.05), m))
        best_c, best_w = None, -1.0
        for i, (ws, w, _m) in enumerate(items):
            cw = sum(wj for wsj, wj, _ in items if abs(wsj - ws) <= 10.0)
            if cw > best_w:
                best_c, best_w = ws, cw
        for ws, _w, m in items:
            if abs(ws - best_c) > 10.0:
                m["selected"] = False


def fill_continuity(results):
    """Rolling mics (config "rolling": true) ran continuously through some
    stretches. If a video sits inside an established file's window but has
    no selected match from that mic, the audio physically exists ->
    add a [CONT] guess clip."""
    windows = []  # (wstart, wend, wav_path, wav_dur)
    byf = {}
    for r in results:
        t0 = _video_start(r)
        if t0 is None:
            continue
        for m in r["matches"]:
            if m["mic"] in ROLLING_MICS and m.get("selected"):
                byf.setdefault(m["wav"], []).append((t0, m, r["video_dur"]))
    for wav, lst in byf.items():
        # established rolling-lav file: >=2 selected matches, or one
        # match covering most of its clip (rules out playback files that
        # only ever grab one fragment)
        big = [x for x in lst
               if (x[1].get("cov_b", 0) - x[1].get("cov_a", 0))
                  >= 0.6 * x[2]]
        if len(lst) < 2 and not big:
            continue
        ws = sorted(t0 - m["shift_sec"] for t0, m, _ in lst)
        w0 = ws[len(ws) // 2]
        windows.append((w0, w0 + lst[0][1]["wav_dur"], wav,
                        lst[0][1]["wav_dur"], lst[0][1]["mic"]))
    for r in results:
        t0 = _video_start(r)
        if t0 is None:
            continue
        # drop stale CONT guesses when a real match for the same file exists
        real_wavs = {m["wav"] for m in r["matches"]
                     if m.get("selected")
                     and m.get("nominated") != "continuity"}
        r["matches"] = [m for m in r["matches"]
                        if m.get("nominated") != "continuity"
                        or m["wav"] not in real_wavs]
        have = {m["wav"] for m in r["matches"] if m.get("selected")}
        blocked = r.get("_blocked", [])
        if r.get("no_fill"):
            continue
        if any(m["mic"] in ROLLING_MICS and m.get("selected")
               for m in r["matches"]):
            continue
        vd = r["video_dur"]
        for w0, w1, wav, wd, wav_mic in windows:
            if (wav not in have
                    and Path(wav).name not in blocked
                    and t0 >= w0 + 1 and t0 < w1 - 1):
                shift = t0 - w0
                r["matches"].append({
                    "mic": wav_mic, "wav": wav,
                    "wav_name": Path(wav).name + " [CONT]",
                    "wav_dur": wd, "shift_sec": shift,
                    "peak": 0, "z": 0, "overlap_sec": 0,
                    "nominated": "continuity", "verified": True,
                    "r_med": 0.25, "n_support": 0,
                    "cov_a": 0.0, "cov_b": min(vd, wd - shift),
                    "time_ok": True, "drift_ppm": None,
                    "drift_ms": None, "selected": True})


def cov_interval(c, vdur):
    """Video-time [a,b) covered by this match (measured by probes)."""
    a = c.get("cov_a")
    if a is None:
        s = c["shift_sec"]
        a = max(0.0, -s)
        b = min(vdur, c["wav_dur"] - s)
        return a, max(a, b)
    return a, c["cov_b"]


def select_coverage(matches, vdur):
    """
    Per mic: sort by z_raw desc, keep a match only if it covers video
    regions the stronger matches don't (mic toggled mid-take).
    Playback duplicates cover the same region -> suppressed.
    """
    out = []
    for mic in MICS:
        taken = []
        for c in sorted((m for m in matches if m["mic"] == mic),
                        key=lambda x: (-(x.get("cov_b", 0)
                                         - x.get("cov_a", 0)) // 15,
                                       -x.get("r_med", 0))):
            a, b = cov_interval(c, vdur)
            covered = sorted(cov_interval(x, vdur) for x in taken)
            new_len = b - a - sum(
                max(0.0, min(b, eb) - max(a, ea)) for ea, eb in covered)
            # drop: out-of-window (valid clock says so = playback echo),
            # or weak r unless coverage is near-full AND time-consistent
            big_cov = (b - a) >= 0.85 * vdur
            c["selected"] = (
                new_len >= max(min(5.0, b - a), 0.4 * (b - a))
                and c.get("time_ok") is not False
                and (c.get("r_med", 0) >= MIN_R_TIMELINE
                     or (big_cov and c.get("time_ok") is not False)))
            if c["selected"]:
                taken.append(c)
            out.append(c)
    return sorted(out, key=lambda c: (c["mic"], c["shift_sec"]))


def selftest():
    rng = np.random.default_rng(0)
    w = np.abs(rng.standard_normal(ENV_FPS * 60)).astype(np.float32)
    w += np.abs(rng.standard_normal(len(w))).astype(np.float32) * 0.05
    v = (w[1000:1300] + rng.standard_normal(300).astype(np.float32) * 0.05)
    lag, peak, z, ov = correlate(v, w)
    assert lag == 1000, f"lag={lag} expected 1000"
    print(f"selftest OK: lag={lag} z={z:.0f} ov={ov:.1f}s")


def render_preview(video: Path, wav: Path, shift: float, vdur: float,
                   out: Path, seg: float = 45.0, v_start: float = None):
    """Render ~seg seconds of video with mic audio swapped in.
    v_start: video time where covered region begins (default: coverage
    start implied by shift)."""
    start_v = max(0.0, -shift) if v_start is None else v_start
    start_w = start_v + shift
    avail = min(vdur - start_v, seg)
    if avail <= 2 or start_w < 0:
        return None
    PREVIEWS.mkdir(exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start_v:.3f}", "-t", f"{avail:.3f}", "-i", str(video),
        "-ss", f"{start_w:.3f}", "-t", f"{avail:.3f}", "-i", str(wav),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out),
    ]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        print("  !! preview failed:", p.stderr.decode(errors="replace")[:300])
        return None
    return out


def render_preview_mix(video: Path, groups, v_start: float, dur: float,
                       out: Path):
    """Render video window [v_start, v_start+dur] with SEPARATE audio
    streams: stream 1 = original camera audio, then one stream per group.
    groups: list of (label, entries) where each entry is a dict with
    'wav' and 'shift_sec' (wav time where video t=0 lands); multiple
    entries in one group are placed at their own offsets on that track.
    The MP4 carries all tracks discretely -> Resolve shows them as
    separate audio tracks."""
    inputs = ["-ss", f"{v_start:.3f}", "-t", f"{dur:.3f}", "-i", str(video)]
    parts = ["[0:a]acopy[cam]"]
    maps = ["[cam]"]
    k = 0  # wav input index
    for gi, (label, entries) in enumerate(groups):
        seg_lbls = []
        for e in entries:
            ws = v_start + e["shift_sec"]        # wav pos at preview start
            delay_ms = max(0.0, -ws) * 1000.0
            w0 = max(0.0, ws)
            inputs += ["-ss", f"{w0:.3f}", "-i", e["wav"]]
            k += 1
            lbl = f"g{gi}s{len(seg_lbls)}"
            parts.append(f"[{k}:a]adelay={delay_ms:.0f}:all=1[{lbl}]")
            seg_lbls.append(lbl)
        if len(seg_lbls) == 1:
            parts.append(f"[{seg_lbls[0]}]anull[g{gi}]")
        else:
            parts.append("".join(f"[{l}]" for l in seg_lbls)
                         + f"amix=inputs={len(seg_lbls)}:normalize=0[g{gi}]")
        maps.append(f"[g{gi}]")
    # stream names (MP4 track title lives in handler_name)
    meta = ["-metadata:s:a:0", "title=Camera (built-in mic)",
            "-metadata:s:a:0", "handler_name=Camera (built-in mic)"]
    for gi, (label, entries) in enumerate(groups):
        files = ",".join(Path(e["wav"]).stem.split("_")[-1]
                         for e in entries)
        meta += ["-metadata:s:a:%d" % (gi + 1),
                 f"title={label} ({files})",
                 "-metadata:s:a:%d" % (gi + 1),
                 f"handler_name={label} ({files})"]
    PREVIEWS.mkdir(exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs,
           "-filter_complex", ";".join(parts),
           "-map", "0:v", *[x for m in maps for x in ("-map", m)],
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-c:a", "aac", "-b:a", "192k", "-ac", "2", *meta,
           "-t", f"{dur:.3f}", str(out)]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        print("  !! track preview failed:",
              p.stderr.decode(errors="replace")[:300])
        return None
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Sync external recorder audio to camera footage and "
                    "emit an FCPXML timeline for DaVinci Resolve.")
    ap.add_argument("day", nargs="?", help="day folder e.g. 2026-07-24, or 'all'")
    ap.add_argument("--config", default=DEFAULT_CONFIG_NAME,
                    help="path to wavesync.config.json "
                         f"(default: ./{DEFAULT_CONFIG_NAME})")
    ap.add_argument("--root",
                    help="override video/audio roots: a folder containing "
                         "'videos' and 'audio' subfolders (test sandbox)")
    ap.add_argument("--timeline-only", action="store_true",
                    help="skip scanning; rebuild fcpxml from existing JSON")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--preview", action="store_true",
                    help="render preview mp4 for best match of each mic")
    ap.add_argument("--only-video", help="restrict to one video filename")
    ap.add_argument("--video-range",
                    help="restrict to a stem range, e.g. C0357-C0380")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.day:
        ap.error("need a day, 'all', or --selftest")

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        ap.error(f"config not found: {cfg_path} — copy "
                 f"wavesync.config.example.json to {DEFAULT_CONFIG_NAME} "
                 f"and set video_root / audio_root / mics")
    load_config(cfg_path)

    vi_dir, se_dir = VI_DIR, SE_DIR
    if args.root:
        r = Path(args.root)
        vi_dir, se_dir = r / "videos", r / "audio"
    days = ([d.name for d in sorted(vi_dir.iterdir()) if d.is_dir()]
            if args.day == "all" else [args.day])
    for day in days:
        if args.timeline_only:
            results = json.loads(
                (REPORTS / f"{day}_sync.json").read_text())
            apply_user_blocks(results, day)
            enforce_file_consistency(results)
            fill_continuity(results)
            (REPORTS / f"{day}_sync.json").write_text(
                json.dumps(results, indent=1))
            videos = [Path(r["video"]) for r in results]
            wavs = [p for m, p in gather_wavs(day, se_dir)]
            fx = write_fcpxml(day, results, videos, wavs,
                              TIMELINES / f"{day}.fcpxml")
            print(f"  -> timelines/{fx.name} (from cached JSON)")
            continue
        vr = (tuple(args.video_range.split("-"))
              if args.video_range else None)
        results = run_day(day, vi_dir=vi_dir, se_dir=se_dir,
                          only_video=args.only_video, video_range=vr)
        if args.preview:
            done = set()
            for r in results:
                if args.only_video and r["video_name"] != args.only_video:
                    continue
                for mic, c in r["best"].items():
                    if mic in done:
                        continue
                    done.add(mic)
                    out = PREVIEWS / day / f"{Path(r['video_name']).stem}__{mic}__{Path(c['wav_name']).stem}.mp4"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    o = render_preview(Path(r["video"]), Path(c["wav"]),
                                       c["shift_sec"], r["video_dur"], out)
                    if o:
                        print(f"  preview: {o}")


if __name__ == "__main__":
    main()
