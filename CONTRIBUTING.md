# Contributing to wavesync

Thanks for helping. This file is short on purpose.

## Set up

```bash
git clone https://github.com/Dvorinka/wavesync.git
cd wavesync
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python wavesync.py --selftest                # proves numpy/scipy work
pytest -q                                    # needs ffmpeg on PATH
```

## Rules of the road

- **One idea per PR.** Bug fix, feature, or refactor - not all three.
- **Never break the FCPXML output.** The timeline XML is the product's
  contract with Resolve; changes to `write_fcpxml` need an import test
  in a real NLE or a very good reason.
- **Correlation code is subtle.** `correlate`, `locate_pair`, and
  `select_coverage` trade precision for recall in non-obvious ways.
  Comment *why*, not *what*, if you touch them.
- **No new dependencies** without discussion in an issue first.
  numpy + scipy + ffmpeg is the whole stack and it should stay small.
- User-facing changes get a `CHANGELOG.md` line under `Unreleased`.

## Tests

`tests/test_e2e.py` synthesizes a recorder WAV and a camera clip with
ffmpeg, runs a real scan, and asserts the offset is found. Add a case
there when you change matching, gathering, or timeline logic.

## Reporting bugs

Open an issue with: the command you ran, the console output, the
`*_sync.json` entry for the misbehaving clip, and file details
(`ffprobe` output helps). Never attach private footage to public
issues - describe the shape of the problem instead.
