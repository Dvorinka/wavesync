# Changelog

## Unreleased

- Web UI restyle: dense NLE-grade layout (Resolve-inspired), timestamped
  console, grouped match table with lane colors, status bar, persistent
  field values, run-locking, auto-loads the report after a scan.

## 0.2.0 - 2026-09-28

### Added
- `wavesync --gui` / `wavesync-gui`: zero-dependency local web UI -
  folder browsing, mic autodetect, config editing, live scan logs,
  match review with block checkboxes.
- `wavesync --init --video-root ... --audio-root ...`: writes a working
  config by inspecting your folders; `--force` overwrites.
- Every container ffmpeg reads is now in scope: MOV/MXF/MTS/AVI/MKV/...
  for cameras, WAV/BWF/MP3/FLAC/M4A/AIFF/OGG/OPUS/... for recorders.
  Configurable via `video_exts` / `audio_exts`.
- Recursive gathering: camera card structures (AVCHD `STREAM/`, nested
  recorder dumps) are traversed, not just top-level files.
- Flat layouts: no day folders required - a bare folder of clips plus a
  bare folder of audio just works.
- `python -m pytest` end-to-end test that synthesizes media with ffmpeg.
- Packaging (`pyproject.toml`): `pip install .` gives `wavesync` and
  `wavesync-gui` commands.

### Fixed
- `--preview` crashed referencing a `best` key that never existed.
- `.WAV` / `.MP4` case variations were silently ignored.
- Recorder folders without a matching day folder contributed nothing;
  they now fall back to a pruned scan of the mic folder.

## 0.1.0 - 2026-08-10

Initial release: two-stage waveform correlation (onset envelope +
raw Pearson probes), broken-clock tolerance, rolling-recorder
reconstruction, playback-echo rejection, blocks-file corrections,
FCPXML timeline export for DaVinci Resolve.
