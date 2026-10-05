# Baleen

**by Walking Whale**

Turn legacy archive files into archival formats, and prove that every one of them is sound.

Baleen walks a folder tree and converts every non-archival file into one of five archival
formats: **JPEG, TIFF, PDF/A, MP4, M4A**. Every output, and every file that was already
archival, is verified (full decode, veraPDF PDF/A validation, full media decode) and recorded
with SHA-256 in a CSV report. **The source folder is never modified**: output goes to a
separate folder that mirrors the source structure.

It runs from a portable folder on macOS (Apple silicon) and Windows 10/11 (x64). Double-click
*Start Baleen* to begin; delete the folder to uninstall. There is no installer, and Baleen
never uses the network.

## Using a release

1. Download `baleen-<version>-win-x64.zip` or `baleen-<version>-mac-arm64.zip` from the
   [releases](https://github.com/czesito/walking-whale-baleen/releases), and check it against
   `SHA256SUMS`.
   - **Windows**: right-click the zip → Properties → **Unblock**, then extract it.
   - **macOS**: after unzipping, run `xattr -dr com.apple.quarantine "<folder>"` once in Terminal.
2. Copy the folder to a local disk and double-click **Start Baleen**. Your browser opens Baleen.
   The console window shows the log; closing it stops Baleen.
3. **Convert**: choose a source folder and an output folder, optionally Preview, then press
   *Start converting*. **Check**: verify a folder read-only.
4. Each run has a page with live progress and then the results. Files that need a person come
   first, each with a "What to do".
5. To uninstall, quit Baleen and delete its folder. Output folders and their `_baleen/` reports
   stay: they are your archival record.

Settings › Resource use controls how much of the computer Baleen may use (Gentle, Balanced,
Maximum or Custom). You can also change it on a running job's page.

## What becomes what

| Source | Becomes |
|--------|---------|
| JPEG, TIFF | copied after a full decode check (never re-encoded) |
| PNG, BMP, GIF, WebP | JPEG, or TIFF when there is transparency, high bit depth or CMYK |
| DOC, DOCX, RTF, ODT, WPD, WPS, SXW, XLS, XLSX, ODS, PPT, PPTX, ODP, TXT, HTML | PDF/A (LibreOffice), validated with veraPDF |
| EML | PDF/A, with attachments extracted and converted beside it |
| PDF | copied if it is valid PDF/A; otherwise flagged |
| AVI, MOV, MPG, WMV, MKV, … | MP4 (copy, remux, or H.264 + AAC) |
| WMA, MP3, WAV, AIFF, FLAC, … | M4A (or MP4) |

Everything else is listed as *Unsupported*, so nothing goes missing from the report.
The full rules are in the [specification](docs/baleen-spec.html).

## Command line

```
python -m baleen serve   [--port N] [--no-browser]
python -m baleen convert <source> <output> [--set key=value]...
python -m baleen check   <folder> [--report PATH]
python -m baleen doctor                        # tool paths and versions
# convert and check also take: --processor gentle|balanced|maximum|N  --memory auto|GB
#                              --transfers auto|N  --work-dir PATH  --no-low-priority  --no-keep-awake
```

Exit codes: 0 all OK/IGNORED · 1 any NEEDS_REVIEW/UNSUPPORTED/SKIPPED · 2 any FAILED ·
3 fatal (bad arguments, lock held, invalid folders).

## Development

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/). The external tools come from a
bundled `runtime/` folder, from environment overrides (`BALEEN_SOFFICE`, `BALEEN_FFMPEG`,
`BALEEN_FFPROBE`, `BALEEN_JAVA_HOME`, `BALEEN_VERAPDF`), or from the system `PATH`.

```
uv venv --python 3.12 .venv
uv pip install --require-hashes -r requirements-dev.lock
uv pip install --no-deps -e .
python -m baleen doctor
python -m pytest tests/unit -q            # unit tests
python -m pytest tests/integration -q     # golden tests against the real tools
python scripts/build_bundle.py --platform win-x64   # portable bundle (see scripts/runtimes.json)
```

- Test fixtures are generated (`tests/fixtures/make_fixtures.py`). The repository never
  contains real archive material.
- The design documents in `docs/` are the source of truth:
  - [spec](docs/baleen-spec.html) for behaviour and IDs;
  - [design](docs/baleen-design.html) for appearance;
  - [prototype](docs/prototype/baleen-prototype.html).
- `STATUS.md` tracks implementation coverage, benchmarks and proposed decision records.
- `docs/manual-test-checklist.md` lists the checks that need a person.

## Privacy

Baleen works fully offline. Reports and logs contain file paths and e-mail headers, so treat
them as carefully as the source material. **Never attach archive files to an issue.**

## Licence

Apache-2.0, copyright 2026 Walking Whale Co., Ltd. See `LICENSE` and `NOTICE`. The bundled
third-party programs (CPython, LibreOffice, FFmpeg, Eclipse Temurin, veraPDF) keep their own
licences; see `THIRD_PARTY_NOTICES/` in each release.

The Baleen and Walking Whale names and logos are trademarks of Walking Whale Co., Ltd. and are
not licensed under Apache-2.0. Logo files live in `assets/brand/`.
