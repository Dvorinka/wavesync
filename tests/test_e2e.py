"""End-to-end tests: synthesize a recorder file and a camera clip with
ffmpeg, run the real CLI, assert the offset is found and the FCPXML
timeline is written. Needs ffmpeg + ffprobe on PATH."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "wavesync.py"
sys.path.insert(0, str(ROOT))

import wavesync  # noqa: E402

FFMPEG = "ffmpeg"


def _ffmpeg(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", *args],
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def shoot(tmp_path_factory):
    """One 90s recorder wav + one 12s clip whose audio is wav[30:42]."""
    tmp = tmp_path_factory.mktemp("shoot")
    day = "2026-08-14"
    vdir = tmp / "videos" / day
    adir = tmp / "audio" / "lav" / day
    vdir.mkdir(parents=True)
    adir.mkdir(parents=True)
    wav = adir / "rec_001.WAV"          # uppercase ext on purpose
    _ffmpeg("-f", "lavfi", "-i",
            "anoisesrc=color=brown:duration=90:seed=7:sample_rate=44100",
            str(wav))
    clip = vdir / "A001.MOV"            # non-mp4 container on purpose
    # wav window is [mtime-90s, mtime]; put the clip inside it so the
    # clock-consistency check can vouch for the match
    ctime = (datetime.now(timezone.utc)
             - timedelta(seconds=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=12",
            "-ss", "30", "-t", "12", "-i", str(wav),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            "-metadata", f"creation_time={ctime}", str(clip))
    cfg = tmp / "wavesync.config.json"
    cfg.write_text(json.dumps({
        "video_root": str(tmp / "videos"),
        "audio_root": str(tmp / "audio"),
        "work_dir": str(tmp / "work"),
        "mics": {"lav": {"folder": "lav", "lane": -1, "rolling": False}},
    }))
    return tmp, cfg, day, wav, clip


def test_scan_finds_offset(shoot):
    tmp, cfg, day, wav, clip = shoot
    p = subprocess.run([sys.executable, str(CLI), day,
                        "--config", str(cfg)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr + p.stdout
    rep = json.loads(
        (tmp / "work" / "reports" / f"{day}_sync.json").read_text())
    assert len(rep) == 1 and rep[0]["video_name"] == "A001.MOV"
    sel = [m for m in rep[0]["matches"] if m.get("selected")]
    assert sel, f"nothing selected: {rep[0]['matches']}"
    best = max(sel, key=lambda m: m.get("r_med", 0))
    assert best["wav"].endswith("rec_001.WAV")
    assert abs(best["shift_sec"] - 30.0) < 0.5
    fx = tmp / "work" / "timelines" / f"{day}.fcpxml"
    assert fx.is_file()
    assert "<asset-clip" in fx.read_text()


def test_gather_accepts_any_container(tmp_path):
    for name, ok in (("a.mov", True), ("b.MTS", True), ("c.mp4", True),
                     ("d.lrv", False), ("e.thm", False),
                     ("notes.txt", False)):
        (tmp_path / name).write_bytes(b"x")
    got = {p.name for p in wavesync.gather_videos(tmp_path)}
    assert got == {"a.mov", "b.MTS", "c.mp4"}


def test_autodetect_flat_and_nested(tmp_path):
    vroot = tmp_path / "vids"
    aroot = tmp_path / "recs"
    (vroot / "2026-08-14").mkdir(parents=True)
    (vroot / "2026-08-14" / "clip.mov").write_bytes(b"x")
    (aroot / "zoom_h5" / "2026-08-14").mkdir(parents=True)
    (aroot / "zoom_h5" / "2026-08-14" / "t1.flac").write_bytes(b"x")
    (aroot / "empty_notes").mkdir()
    (aroot / "empty_notes" / "readme.txt").write_bytes(b"x")
    cfg = wavesync.autodetect_config(vroot, aroot, tmp_path / "w")
    assert "zoom_h5" in cfg["mics"]
    assert "empty_notes" not in cfg["mics"]
    assert wavesync.list_days(vroot) == ["2026-08-14"]

    flat_v = tmp_path / "flatv"
    flat_a = tmp_path / "flata"
    flat_v.mkdir(); flat_a.mkdir()
    (flat_v / "clip.mp4").write_bytes(b"x")
    (flat_a / "take.mp3").write_bytes(b"x")
    cfg2 = wavesync.autodetect_config(flat_v, flat_a)
    assert cfg2["mics"] == {"recorder": {"folder": ".", "lane": -1,
                                        "rolling": False}}


def test_init_cli(tmp_path):
    vroot = tmp_path / "v"; aroot = tmp_path / "a"
    vroot.mkdir(); aroot.mkdir()
    (vroot / "x.mp4").write_bytes(b"x")
    (aroot / "y.wav").write_bytes(b"x")
    out = tmp_path / "c.json"
    p = subprocess.run([sys.executable, str(CLI), "--init",
                        "--video-root", str(vroot),
                        "--audio-root", str(aroot),
                        "--config", str(out)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    cfg = json.loads(out.read_text())
    assert cfg["mics"], "no mics detected"
    # refuse to clobber without --force
    p2 = subprocess.run([sys.executable, str(CLI), "--init",
                         "--video-root", str(vroot),
                         "--audio-root", str(aroot),
                         "--config", str(out)],
                        capture_output=True, text=True)
    assert p2.returncode != 0
