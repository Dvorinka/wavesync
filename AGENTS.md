# AGENTS.md — operating playbook for coding agents

wavesync is designed to be driven by an agent + human pair: the agent runs
scans and interprets reports; the human knows which mics were used and judges
ambiguous clips by ear in Resolve.

## Driving the tool

- Always pass `--config <path>` (or run from a dir with wavesync.config.json).
- `python wavesync.py --selftest` — zero-config sanity check; run this first
  in a new environment to prove numpy/scipy exist (e2e tests need ffmpeg).
- `pytest -q` — e2e suite synthesizes media with ffmpeg; run after touching
  matching, gathering, or timeline code.
- `python wavesync.py --gui` / `wavesync_gui.py` — stdlib web UI on
  127.0.0.1:8710; scans run as subprocesses so the CLI is the truth.
- `--init --video-root V --audio-root A` writes a config by autodetect;
  media sets live in `VIDEO_EXTS`/`AUDIO_EXTS` (config `video_exts`/
  `audio_exts` override).
- `--timeline-only` regenerates reports+XML from the cached scan — use it
  after editing a `*_blocks.json` instead of rescanning.
- `--video-range C0xxx-C0yyy` for incremental day processing; results merge
  into the day report (earlier videos are preserved).
- Never edit generated `*_sync.json` matches by hand except to inject a
  verified placement — give injected entries `"nominated": "manual"` or
  `"continuity"` so merges preserve them.

## Match semantics

- `selected: true` + high `r_med` → trustworthy.
- `cov_a..cov_b` = seconds into the VIDEO the wav provably covers, not wav
  time. `shift_sec` = wav position where video t=0 lands.
- `time_ok: false` = recorder clock valid and says the wav wasn't recording
  during the clip → echo/unrelated. `time_ok: null` = clock untrusted.
- Many weak hits on the SAME window from many different files = playback
  echo fingerprint, not real audio.
- `[CONT]`/`[GUESS]` names are review markers for the human.

## Corrections

Write `work_dir/reports/<day>_blocks.json` — never delete matches from the
JSON directly; blocks survive regenerations, manual edits don't.

## Pitfalls learned the hard way

- If a recorder's mtimes are garbage (1999, 2029, epoch), that's normal —
  `time_consistent` returns null and correlation carries it.
- A recorder folder may be missing for a day (empty dir) — the tool just
  has fewer candidates; not an error.
- `<day>_blocks.json` `"*"` = camera-only clip, permanently.
- Don't run two scans concurrently — shared cache writes aren't locked.
