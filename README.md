# wavesync

Sync external audio recorder files to camera footage automatically, and get
an **FCPXML timeline you can import straight into DaVinci Resolve** — camera
audio intact, each recorder on its own track, everything at its true position.

Built for the classic headache: a shoot where the camera rolls in takes while
lavs and prop mics start, stop, and restart independently. Instead of syncing
hundreds of clips by hand, wavesync finds which WAV covers each video by
waveform correlation, reconstructs continuous recorder sessions even when the
recorder's clock is wrong, and writes a ready-made timeline.

## What it does

- **Waveform matching** — two stages: onset-envelope correlation nominates
  candidates, then raw-waveform Pearson probes at energetic moments
  (clapperboards, door slams) verify them.
- **Broken-clock tolerant** — recorders with unset or wildly-wrong clocks
  (files stamped 1999 or 2029) still match; file timestamps are only used
  when they're sane.
- **Rolling sessions** — a recorder that stays on through several takes is
  detected as one file spanning multiple videos, and placed as multiple
  references into the same WAV at different offsets.
- **Quiet audio** — analysis decodes run through FFmpeg `dynaudnorm`, so
  whisper-level lav audio correlates. Source files are never modified.
- **Playback-echo rejection** — in-scene playback re-records earlier takes;
  temporal-consistency + coverage checks keep the real file and drop echoes.
- **True positions, real gaps** — clips never shift to fit. If two recordings
  genuinely overlap, the later one drops to a lower lane.
- **Camera fallback** — clips with no external match keep their camera audio.
- **Orphan parking** — WAVs recorded during a take that didn't match get
  parked on a spare lane at their true position; everything else lands in the
  project bin (imported, off the timeline).
- **Manual overrides that stick** — `reports/<DAY>_blocks.json` holds your
  corrections; regenerating never resurrects rejected clips.

## Requirements

- Python 3.10+, `numpy`, `scipy` (`pip install -r requirements.txt`)
- `ffmpeg` + `ffprobe` on PATH

## Layout

```
videos/2026-08-14/C0448.MP4 ...      one folder per shooting day
audio/lav/2026-08-14/*.wav           one folder per recorder per day
audio/prop/2026-08-14/*.wav
```

Day folder names must match between `video_root` and each mic folder
(unless remapped via `day_map`).

## Setup

```bash
pip install -r requirements.txt
cp wavesync.config.example.json wavesync.config.json
# edit wavesync.config.json: video_root, audio_root, work_dir, mics
```

## Usage

```bash
python wavesync.py 2026-08-14              # scan one day -> timeline
python wavesync.py all                     # every day folder
python wavesync.py 2026-08-14 --video-range C0448-C0476
python wavesync.py 2026-08-14 --only-video C0448.MP4
python wavesync.py 2026-08-14 --timeline-only   # rebuild XML from report
python wavesync.py --selftest              # sanity check, no config needed
```

Then import `work_dir/timelines/<DAY>.fcpxml` in Resolve
(File → Import → Timeline). Re-run after corrections — your blocks persist.

## Reading the report

Each clip's `matches` carry `selected` plus a coverage span `cov_a..cov_b`
(seconds into the video the WAV provably covers). Clip names in Resolve get
tags:

- `[CONT]` — placed by continuity (a rolling recorder was provably running;
  content too quiet to verify). Listen before trusting.
- `[GUESS]` — a weak/ambiguous candidate placed for review.
- `AUDIO-ONLY` — recorded during the take but not matching; parked to keep.

## Correcting mistakes — blocks file

`work_dir/reports/<DAY>_blocks.json` (created on first use):

```json
{
  "C0448.MP4": ["recorder_012.wav"],
  "C0476.MP4": ["*"]
}
```

- A WAV name keeps that file off that clip (match *and* parking).
- `"*"` means: this clip is camera-only.
- After editing, regenerate: `python wavesync.py <DAY> --timeline-only`

## Config keys

| key | meaning |
|---|---|
| `video_root` | folder containing day folders of videos |
| `audio_root` | folder containing one subfolder per recorder |
| `work_dir` | where reports/timelines/cache/previews go |
| `mics` | `{"name": {"folder": str, "lane": int, "rolling": bool}}` — `rolling` mics get continuity fills for quiet stretches |
| `day_map` | `{"mic": {"<video_day>": "<audio_day>"}}` for mislabeled days |
| `skip_dirs` | subfolder names ignored when gathering WAVs |
| `skip_prefixes` | filename prefixes ignored |
| `analysis_filter` | FFmpeg `-af` applied to analysis decodes only |
| `sample_rate` | analysis decode rate (default 8000) |
| `min_r_timeline` | correlation floor for timeline placement |

## How it works (short version)

1. Decode each WAV + video audio track to mono at `sample_rate` (cached).
2. Onset envelopes (spectral flux) cross-correlate → candidate shifts.
3. Verified candidates get raw-audio Pearson probes at energetic peaks,
   then swept inside the implied overlap to measure coverage.
4. Per-mic coverage selection keeps multiple segments (recorder toggled
   mid-take) and suppresses duplicates/echoes covering the same span.
5. File-consistency pass reconstructs each file's real recording window
   across all clips — echoes imply scattered windows and get dropped.
6. Continuity fill: videos inside a proven rolling window get `[CONT]`.
7. `write_fcpxml` emits Resolve's own XML dialect — `media-rep` children,
   source timecode domains, collision-free lanes.

## License

MIT — see LICENSE.
