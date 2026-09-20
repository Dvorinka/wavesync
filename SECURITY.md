# Security Policy

wavesync is a local command-line tool. It shells out to `ffmpeg`/`ffprobe`
on media paths from your config and writes files only under `work_dir`.
The web UI (`wavesync --gui` / `wavesync_gui.py`) binds to `127.0.0.1`
only - it is meant for the machine it runs on, not for exposure to a
network.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Contact the
maintainer privately (see the repository profile for contact details)
with a description and reproduction. Expect a response within a few days.
