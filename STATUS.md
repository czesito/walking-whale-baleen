# Baleen: implementation status

Status of v0.1.0 against Specification v1.2 and Design v1.1, as of 2026-10-06.

## Summary

- **Everything is built.** The whole v1 pipeline works on real tools: images, documents, plain text,
  HTML, e-mail with attachments, existing PDFs, audio and video. Every output is verified (V-IMG,
  V-PDF-OPEN, V-PDFA with real veraPDF, V-TEXT, V-AV-*, V-HASH) and recorded in the CSV report, run JSON
  and journal.
- **The UI covers every design state.** All W-01…W-23 are implemented on the local server with the full
  security model (SEC-1…SEC-11), and are verified with 37 Playwright tests in Chromium against the
  prototype.
- **Both portable bundles build and pass their smoke tests.** win-x64 builds locally and in CI; mac-arm64
  builds in CI. Runtimes are pinned with vendor SHA-256: CPython 3.12.15, LibreOffice 26.8.0.3, Temurin
  JRE 21.0.12.1 and veraPDF 1.30.2. FFmpeg 9.0.2 + x264 r3223 is built by Baleen from source,
  GPL-2.0-or-later.
- **CI is green.** `test.yml` runs on ubuntu, windows and macOS, including the integration suite again
  with networking disabled and 0 outbound attempts (AC-10). `bundle.yml` is green for win-x64 and
  mac-arm64.
- **Locally the full suite passes**: unit tests, integration tests with every tool required, and
  Playwright. Benchmarks meet AC-13, AC-15 and AC-16 on this machine. AC-11 passes locally.
- **An independent review found 11 issues**, including one blocker (cancel while planning). Ten are
  fixed and covered by regression tests. The remaining one, SMB access from UNC-linked pictures in
  .doc files, is listed under Known gaps.

## What works and how to run it

**Release bundle (Windows x64, macOS Apple silicon).**
1. Unzip `baleen-0.1.0-<platform>.zip` and double-click *Start Baleen*. The launcher sets
   `BALEEN_HOME`, `TEMP`/`TMPDIR` → `data/tmp` and the Java preferences → `data/java`, then runs
   `runtime/python … -m baleen serve` and opens the browser at the token URL.
2. Convert, Check, Runs, Tools, Settings and About work as the design describes. Quit is in the rail.
3. To uninstall, delete the folder. AC-11 checks that nothing else remains.

**From a source checkout.**
```
uv venv --python 3.12 .venv
uv pip install --require-hashes -r requirements-dev.lock
uv pip install --no-deps -e .
set BALEEN_RUNTIME=<path to a bundle's runtime folder>   (optional; else tools from PATH)
python -m baleen doctor
python -m baleen serve
python -m baleen convert <source> <output> [--set key=value] [--processor gentle|balanced|maximum|N]
python -m baleen check <folder> [--report PATH]
```

**Tests.**
```
python -m pytest tests/unit -q                       # about 590 tests, about 40 s
set BALEEN_REQUIRE_TOOLS=1
python -m pytest tests/integration -q                # golden + acceptance, about 15 min, real tools
python -m pytest tests/ui -q                         # Playwright (Chromium), about 75 s
```
For the media fixtures with the minimal release FFmpeg, set `BALEEN_FIXTURE_FFMPEG` to a full FFmpeg
(the MP3 fixture needs LAME, which Baleen itself never uses).

**Build and measure.**
```
python scripts/build_bundle.py --platform win-x64 --ffmpeg-from <ffmpeg.yml artifact> [--cache DIR]
python scripts/make_benchmark.py <dir>               # 2,000 JPEGs, 200 documents, 40 videos
python scripts/bench.py <dir> --out results.json --profiles balanced,gentle,maximum,8,1
python scripts/ac11_local.py dist/baleen-0.1.0-win-x64.zip --work %TEMP%/baleen-test/ac11
```

## Verification

| Suite | Where | Result |
|---|---|---|
| Unit (594) | local, CI ubuntu/windows/macOS | pass (`test.yml` green) |
| Integration (47): golden `expected.csv` for every fixture group and profile, AC-07, AC-08 (kill mid-LibreOffice and mid-FFmpeg), UNC, R-05 listener, CJK/R-02 | local with all tools required (bundle runtime, self-built FFmpeg); CI ubuntu with pinned LibreOffice 26.8 + veraPDF 1.30.2 | pass |
| Integration offline (AC-10) | CI ubuntu, network namespace, packets of the test user counted | pass, 0 outbound attempts |
| UI, Playwright (37): every W-state, keyboard-only Convert/Check, serve to quit | local, Chromium | pass |
| Bundle build + smoke (doctor, convert of the smoke set) | win-x64 local and CI; mac-arm64 CI | pass |
| AC-11 clean uninstall | win-x64 local (`scripts/ac11_local.py`); win-x64 + mac-arm64 in `bundle.yml` | pass (only background apps' writes; see below) |
| Benchmarks (AC-13, AC-15, AC-16) | local | pass (see Benchmarks) |
| Independent review | fresh agent against AC-01…AC-16, SEC, design | 11 findings, 10 fixed (see below) |

## Spec ID coverage

| Area | IDs | Implemented | Tested | Not verified |
|---|---|---|---|---|
| Principles | P1–P9 | all | source snapshots in every golden run (AC-01); P2/P4/P5 engine tests; junction and P1 regression; publish intents (AC-08); every item exactly once (AC-02) | P8 residual: UNC links in .doc (Known gaps) |
| Goals / non-goals | G1–G6, NG1–NG8 | all | through the ACs | — |
| Pipeline | §5.1–§5.6, DR-32…DR-35 | all | `test_runner`, `test_scheduler` (JS oracle for the §5.5 formulas, 3,000 cases), AC-08/AC-09, recovery, live-run lock, batching regression | — |
| Formats | §6.1–§6.7 | all routes | golden groups images, text, html, pdf, documents, email, media, other, names | R-06 layout fidelity (manual) |
| Naming | §7.1–§7.8, DR-05, DR-18 | all | `test_plan` (clash table, shuffles, attachments vs source folders), golden names, resume (AC-07) | reserved names (R-09) not tested |
| Verification | V-IMG, V-PDF-OPEN, V-PDFA, V-TEXT, V-AV-PROBE, V-AV-DUR, V-AV-DECODE, V-HASH | all | golden `checks` column on real tools | — |
| Statuses | §9 (all 36 codes) | all | code table = spec table; most codes exercised by golden rows | — |
| Report | §10.1–§10.4 | all | CSV format (BOM, CRLF, RFC 4180, plan order), run JSON, run index, journal | — |
| Settings | §11 | all | validation, overrides, live resource changes, load warnings shown on Settings | — |
| UI | UI-G1–G4, UI-C1–C6, UI-K1–K2, UI-R1–R8, UI-H1–H3, UI-T1, UI-S1–S2, UI-A1, §12.8, §12.9 | all | Playwright per W-state; route and unit tests; dummy workflow without template changes | native dialogs, Safari/Firefox/Edge (manual) |
| Security | SEC-1–SEC-11 | all | `test_d_security` (AC-12), LAN-address refusal, reviewer pass | SEC-9 residual (UNC); SEC-10 no sandbox by design |
| Packaging | §14.1–§14.7 | all | bundles + smoke in CI and locally; AC-11 | launcher double-click on a real Mac (manual) |
| Licensing | §15, DR-22 | all | notices generated per component; GPL sources attached by `bundle.yml` | — |

## Acceptance criteria

| AC | Status | Evidence |
|---|---|---|
| 01 Source untouched | automated | source snapshot (listing, size, mtime, SHA-256) before and after every golden run; engine tests; junction regression |
| 02 Completeness | automated | golden: every row is expected and appears exactly once; engine tests |
| 03 Integrity | automated | golden: every output file matches its row's SHA-256, and there is no output for FAILED/UNSUPPORTED/IGNORED/SKIPPED; also after a crash (AC-08) |
| 04 Formats | automated | golden: archival extensions only; no VALIDATOR_MISSING when tools are required |
| 05 Naming | automated | golden names group (§7.3 table, CJK, emoji, 255-unit names, PATH_TOO_LONG) |
| 06 PDF/A 1b/2b/3b | automated | `pdfa_1b` / `pdfa_3b` profiles with real veraPDF (CJK caveat: proposed DR-52) |
| 07 Idempotent | automated | `test_ac07_and_ac09_with_real_tools`; media and engine re-runs: RESUMED, no changes |
| 08 Crash-safe | automated | kill mid-LibreOffice and mid-FFmpeg inside the run's own Job Object, then re-run: completes, no partial files, no staging left; publish intents |
| 09 Cancel | automated | engine test (remaining rows SKIPPED CANCELLED, report + run JSON); cancel while planning (regression) |
| 10 Offline | automated, CI | offline integration run, 0 outbound attempts (Linux only) |
| 11 Clean uninstall | automated | local win-x64 (`scripts/ac11_local.py`): no new registry entries and no tool-related paths. Remaining changes are Edge, OneDrive, VS Code and Discord background writes, plus one transient GUID `.tmp` its owner deleted. Same check in `bundle.yml` for both platforms |
| 12 Security | automated | `test_d_security`, `test_d_serve` |
| 13 Performance | measured | 2,000 JPEGs (1.1 GB) check + copy in 10.0 s (limit 5 min); Preview of 2,000 files 0.21 s (limit 10 s). On a much faster machine than the spec's 2020 laptop |
| 14 UI keyboard | partly automated | Convert and Check keyboard-only in Chromium (Playwright); Safari, Firefox and Edge are manual |
| 15 Resource limits | measured | busiest 30 s ≤ B + 0.5 at Gentle, Balanced, Maximum, custom 8 and custom 1; reserved memory ≤ M; transfers ≤ T; live change at next dispatch (unit test) |
| 16 Throughput | measured | Balanced 110.6 s vs custom 1 885.3 s: 8.0× (needs ≥ 2.5×). Measured on 28 threads; the spec's 8-core Mac and PC are not measured |

## Benchmarks

**Machine.** Intel Core i7-14700 (8 P-cores + 12 E-cores, 28 logical processors), 80 GB RAM, Windows 11
Pro 10.0.26200. Data on the internal SSD (`%TEMP%`). Microsoft Defender real-time protection was on for
every measurement. Its effect on output writes can't be isolated without adding exclusions, which this
session was not allowed to do.

**Set.** `scripts/make_benchmark.py`: 2,000 JPEGs (1.1 GB), 100 DOCX + 100 RTF of about 3 pages, and 40
ten-second 640×480 videos (20 MJPEG AVI, 10 MPEG-1, 10 H.264 MP4); 2,240 items in all. Runtime: the release
bundle's (LibreOffice 26.8.0.3, self-built FFmpeg 9.0.2, veraPDF 1.30.2). CPU is the whole process tree's
CPU time, measured through a Job Object, so exited children count.

| Profile | B | Wall | Average cores | Busiest 30 s | Limit B+0.5 | Peak reserved memory |
|---|---|---|---|---|---|---|
| Balanced | 14 | 110.6 s | 4.77 | 7.78 | 14.5 | 5.6 GB (M = 20 GB) |
| Gentle | 7 | 129.6 s | 3.26 | 4.43 | 7.5 | 4.0 GB |
| Maximum | 27 | 106.1 s | 5.02 | 8.05 | 27.5 | 5.6 GB |
| custom 8 | 8 | 104.6 s | 4.23 | 4.92 | 8.5 | 5.0 GB |
| custom 1 | 1 | 885.3 s | 0.92 | 1.18 | 1.5 | 1.0 GB |

- **AC-16:** Balanced / custom 1 = **8.0×**, against the required 2.5×. All 2,240 items were OK in every
  run.
- **Lower priority on vs off** (two repeats each, means):
  - Balanced 103.9 / 99.6 s (+4 %);
  - Maximum 99.9 / 99.1 s (+1 %);
  - custom 8 104.1 / 101.9 s (+2 %).

  Below-normal priority, and any E-core placement Windows applies to it, doesn't slow Baleen materially
  on an otherwise idle machine. No DR is proposed.
- **Bottleneck.** Documents take about 70 s first-to-last in every profile, limited by K ≤ 4 LibreOffice
  instances at about 1.2–1.4 s per document. In Balanced and Maximum the two video encodes hold ⌊B/F⌋
  tokens each, which is all of B, so documents only start once the encodes finish (videos 16–50 s,
  documents 39–109 s). See proposed DR-57.
- **Two bugs found by benchmarking, now fixed.**
  - Batches never formed (DR-35). The first round took 420–485 s in every profile, with documents
    finishing one at a time. The fix made Balanced 3.8× faster and cut total CPU from 2,821 s to 527 s.
  - LibreOffice and the JVM ran internal threads beyond their single token. custom 1 measured 2.01 cores
    against the 1.5 limit; with the caps (proposed DR-58) it measures 1.18.
- **AC-13:** check + copy of 2,000 JPEGs took 10.0 s, check-only 4.9 s, and Preview 0.21 s (the whole
  2,240-item set 0.46 s, including FFprobe on 40 videos).

## Independent review

A fresh agent that had not seen the build reviewed the code against AC-01…AC-16, SEC-1…SEC-11 and the
design. It confirmed the security middleware, reveal containment, the subprocess runner, the
`.part` + V-HASH + no-replace publish, the DR-08/DR-09 outcome logic, the CSV format and the §5.5
formulas. Its findings and their outcome:

| # | Severity | Finding | Outcome |
|---|---|---|---|
| 1 | blocker | Cancel while scanning or planning executed a half-probed plan (JPEGs re-encoded, published as OK) | fixed: nothing runs after a pre-execution cancel; regression test |
| 2 | major | Crash recovery in a second Baleen process finalised a live run | fixed: per-run lock; regression test |
| 3 | major | Publish followed a junction inside the output root into the source (P1) | fixed: real-path containment before creating folders or renaming; Windows junction regression test |
| 4 | major | An attachments folder and a source folder of the same name escaped the clash rule | fixed: folders resolve depth by depth; regression test |
| 5 | major | One unreadable subfolder aborted the run; drive roots always failed | fixed: one FAILED row; System Volume Information is a system folder (proposed DR-55) |
| 6 | minor | Attachments of an e-mail that failed before releasing them became INTERRUPTED | fixed: reported with the parent's failure |
| 7 | minor | A folder at the output path gave SOURCE_UNREADABLE | fixed: OUTPUT_OCCUPIED |
| 8 | minor | A crash between rename and journal commit left an unreported output | fixed: publish intent + recovery reconciliation; journal `synchronous=FULL` |
| 9 | minor | Invalid `settings.json` warnings only in the log | fixed: warning notice on the Settings page; test |
| 10 | major | UNC-linked pictures in .doc open SMB connections (NTLM exposure on Windows) | open (Known gaps) |
| 11 | minor | A journal rebuilt into the output folder on a GET | fixed: rebuilt into `data/cache` |

It also pointed out two test weaknesses, both fixed: AC-02 is now checked for duplicates, and AC-08 had a
no-op assertion.

## Proposed decision records

Judgement calls made during implementation. Each one is a proposal: nothing in the
approved documents was edited. Accept, amend or reject them in a new spec revision.

### DR-38 (proposed): `BALEEN_RUNTIME` overrides the runtime folder

- **Decision.** `BALEEN_RUNTIME=<dir>` makes Baleen look for bundled tools in `<dir>` instead of
  `<BALEEN_HOME>/runtime`. It is a development and test convenience, not a user setting, and it does
  not appear in the UI.
- **Why.** A source checkout, several git worktrees and CI jobs can share one roughly 1 GB runtime
  without copying it, while each keeps its own `data/`.
- **Rejected.** Setting five tool overrides (`BALEEN_SOFFICE`, …) in every test: verbose, and it skips
  the "bundled" lookup path that the release actually uses.

### DR-39 (proposed): content-dependent targets are decided at plan time

- **Decision.** The planner runs the route probes (Pillow headers, FFprobe, e-mail parsing) in parallel
  before any write. Three things are therefore known when the clash rule runs: a PNG's `.jpg`/`.tif`
  target, the video/audio route and `.mp4`/`.m4a`, and e-mail attachments. Probes are read-only and use
  the Files-lane parallelism (B threads).
- **Why.** §7.3 computes clashes over full target names, and Preview (UI-C4) must show the real plan.
  Probing during the run could rename a file after the clash rule had already been applied.
- **Rejected.** Extension-only planning with a later "re-plan" (unstable names, P7).

### DR-40 (proposed): attachments are planned under every e-mail policy, emitted only under `extract`

- **Decision.** Attachments are always enumerated for the clash computation. Under `block` and `list`
  they get no report row (`materialise=false`).
- **Why.** §7.3: "names are stable across settings". A source folder named `mail_attachments/` beside
  `mail.eml` gets the same names whatever the policy.

### DR-41 (proposed): e-mail nesting depth counts the top-level message as level 1

- **Decision.** a.eml (1) ⊃ b.eml (2) ⊃ c.eml (3) are processed. A message at level 4 is NEEDS_REVIEW
  `EML_NESTING_TOO_DEEP` and is not converted. Matches the §16.1 fixture "4-level nesting →
  EML_NESTING_TOO_DEEP".

### DR-42 (proposed): cancel lets started items finish through Publish

- **Decision.** Cancel removes the queued Stage/Hash tasks of items that have not started. Items already
  staged ("files in progress") continue through convert, verify and publish. Never-started items become
  SKIPPED `CANCELLED`.
- **Why.** "Stopping after n files in progress…" (§5.6) and no wasted conversions. Atomicity (P5) is
  unaffected.

### DR-43 (proposed): every regular file is hashed, including unsupported and plan-time decisions

- **Decision.** `source_sha256` is computed for every scanned regular file except IGNORED ones:
  UNSUPPORTED files, plan-time NEEDS_REVIEW items, and NOT_ARCHIVAL_FORMAT in Check mode.
- **Why.** G5 asks for fixity "for every file". Check mode promises "SHA-256 for every file" (UI-K2).

### DR-44 (proposed): report `action` for items decided at plan time

- **Decision.** `none` in Convert runs (nothing was attempted). `check` in Check runs, because the file
  was hashed. RESUMED and OUTPUT_INVALID rows use `check` and carry the kept file's `output_path`,
  `output_size` and `output_sha256`, so AC-03 holds on re-runs.

### DR-45 (proposed): Stopped runs and lock ownership

- **Decision.**
  - A fatal mid-run condition (the output folder disappears) stops dispatching. Remaining items become
    SKIPPED `INTERRUPTED`. If the output folder can't take the report, it is written to `data/reports/`
    and the run page says so (UI-R7).
  - A lock file from another hostname is treated as held, because liveness can't be checked across
    machines. The message tells the user which file to delete if no other Baleen is running.

### DR-46 (proposed): repository layout refinements

- **Decision.** `verify.py` became the package `verify/` (`image.py`, `pdf.py`, `media.py`) so that
  workstreams don't share one file.
- **Added modules.**
  - `convert/base.py`: the route contract.
  - `convert/formats.py`: the §6 table.
  - `convert/libreoffice.py`: batches.
  - `convert/sanitize.py`: §6.4.
  - `journal.py`, `fsops.py` (no-replace rename, lock), `proc.py` (subprocess runner),
    `osutil.py` (priority, keep-awake), `home.py`, `paths.py`, `logs.py`, `cli_run.py`.
- **New setting.** The `check_dir` workflow key stores the Check workflow's last folder.

### DR-47 (proposed): aging so multi-token tasks are not starved

- **Decision.** Plan order still decides within a lane (§5.5), and lanes still backfill. But a head task
  that has waited more than 2 s without fitting its budget holds back later-ordered tasks in other lanes
  until it fits.
- **Why.** A Media task needs ⌊B/F⌋ tokens at once. Without aging, a steady stream of 1-token
  Files tasks from later items keeps taking freed tokens, and the encode waits until the Files queue
  drains. This was demonstrated by `test_multi_token_task_is_not_starved_by_file_tasks`.

### DR-48 (proposed): Baleen builds its own minimal FFmpeg (resolves R-08)

- **Finding.** No suitable pinned static build with only FFmpeg + x264 exists for both platforms. The
  usual builds (Gyan "essentials" on Windows, martin-riedl.de on macOS) are configured with
  `--enable-version3`, making them GPL-3.0-or-later. They statically link further GPL libraries (x265,
  xvid, …) whose exact sources cannot all be obtained (e.g. an x265 commit that is not in the upstream
  repository). Shipping them would mean distributing GPL binaries without their corresponding source.
- **Decision.**
  - `.github/workflows/ffmpeg.yml` builds FFmpeg 9.0.2 + x264 r3223 (0480cb05) from pinned source
    archives with `scripts/build_ffmpeg.sh`. win-x64 is cross-compiled with mingw-w64 (zlib 1.3.2
    linked); mac-arm64 is built natively.
  - Configuration: `--enable-gpl --enable-libx264`, no `version3`, no `nonfree`, no network, no capture
    devices, and only the lavfi input. `ffmpeg -L` states GPL version 2 or later.
  - `scripts/check_ffmpeg.py` verifies the licence, the §6.7 decoders, encoders and muxers, and a test
    encode.
  - Bundles take the binaries with `build_bundle.py --ffmpeg-from`. The source archives (FFmpeg, x264,
    zlib) and the build script are attached to each release.
  - The Gyan and martin-riedl pins remain for development only.
- **Verified.** The media golden tests pass with the self-built binaries.

### DR-49 (proposed): Java paths and the Windows ANSI code page (R-02)

- **Finding.** On Windows without the system-wide "UTF-8 for worldwide language support" option, the
  Java launcher goes through the ANSI code page:
  - Java cannot start from a Baleen folder whose path has characters outside it (e.g. CJK on cp1252);
  - CJK file names passed to veraPDF arrive as `??`.
- **Decision.**
  - When the Baleen folder's path is not ANSI-representable, Tools reports Java and veraPDF as missing,
    with a "move the folder" fix, so PDFs are honestly VALIDATOR_MISSING. The launcher warns about it.
  - veraPDF inputs whose path is not ANSI-representable are first staged under an ASCII name in the run's
    work folder (Check mode and attachments included).
  - README.txt tells users to keep the Baleen folder on a Latin-letter path.
- **Why.** No system settings may be changed (the UTF-8 option is a system setting). Source names are
  never changed (§7.2), so staging a copy is the only safe route.

### DR-50 (proposed, media): from workstream (c)

1. **Timeout.** max(600 s, 10 × duration) is too short for grainy 720p sources at 1 thread (measured: 30 s
   of grainy 720p took 390 s at `-threads 1`, so clips over about 46 s would TIMEOUT). Proposal: scale the
   limit with pixel rate and thread count, e.g. max(600 s, 10 × duration × max(1, pixels/921,600) ×
   max(1, 4/threads)).
2. **Per-stream copy.** Copy decisions are made per stream. An AVCHD clip keeps its H.264 video and only
   its AC-3 audio is re-encoded to AAC (action `convert`). This follows DR-12 ("avoid generation loss").
3. **Interlacing and range.** Interlaced sources stay interlaced (§6.7). Transcodes are tagged TV range,
   because FFmpeg 9 otherwise keeps MJPEG's full range.
4. **V-AV-DUR.** When FFprobe only estimates the duration (e.g. a VBR MP3 without a Xing header, probed as
   20.05 s for 6.03 s), the duration is taken from an exact packet count.
5. **Check mode.** `.m4a` and audio-only `.mp4` are accepted whatever `audio_container` says.
6. **No streams.** Playlist and still-image containers are UNSUPPORTED `NO_MEDIA_STREAMS`. MPEG-TS `.ts`
   files that are really TypeScript sources fail probing (FAILED `SOURCE_UNREADABLE`). The spec lists
   `.ts` as video, so this is expected but worth a note in the docs.

### DR-51 (proposed, images/text/HTML/PDF): from workstream (a)

1. **HTML images.** Headless LibreOffice fetches linked images but never renders them. Baleen therefore
   inlines relative images as `data:` URIs, read-only and only from inside the source root. Any other
   reference becomes placeholder text (§6.4, R-05).
2. **V-TEXT.** Characters are compared as an NFKC multiset, not a plain count. A count alone passes
   mojibake, which was verified.
3. **Encodings.** big5 → cp950 and shift_jis → cp932 (the Windows supersets); a UTF-32 BOM counts as
   certain. HTML charset labels follow WHATWG. A declared charset that fails to decode falls back to the
   §6.3 rules.
4. **Check mode.** Text files are NOT_ARCHIVAL_FORMAT without applying the encoding rules.
   MULTI_FRAME_IMAGE is reported in both modes.
5. **Grey ICC.** Greyscale images with a GRAY ICC profile become greyscale JPEGs, so the kept ICC stays
   valid.
6. **veraPDF.** Every file is staged under an ASCII name in a batch folder (R-02). A veraPDF PARSE error
   means fail. A job without a result means unavailable (VALIDATOR_ERROR). The batch timeout is
   `verapdf_timeout_s` + 30 s per extra file.
7. **Incomplete claims.** An incomplete PDF/A claim (part without conformance) is PDFA_INVALID at plan
   time.
8. **Known limit.** Pillow reads 16-bit colour PNGs as 8 bits per channel. Such files go to TIFF with a
   message.

### DR-52 (proposed): PDF/A-1b and CFF (OpenType) CJK fonts

- **Finding (CI, Linux).** For CJK text, LibreOffice 26.8 embeds a CFF-flavoured OpenType font (e.g. Noto
  CJK) as a Type 1 subset without the CharSet string that PDF/A-1 requires. veraPDF rejects it (rule
  6.3.5), so Baleen correctly reports FAILED `VERIFY_FAILED` and places nothing.
- **Unaffected.** TrueType fonts, which are the Windows CJK fallbacks, pass. PDF/A-2b and 3b, where the
  CharSet rule is optional, pass.
- **Decision.**
  - Keep the default 2b (DR-07).
  - Document that 1b with CJK text can fail on systems whose CJK fonts are CFF (Linux; possibly macOS,
    e.g. Hiragino). This is on the manual checklist for a real Mac.
  - CI uses a TrueType CJK font so its 1b golden rows test Baleen rather than the font stack.
- **Also found.** Ubuntu's own LibreOffice 24.2 writes PDF/A-1b with a CreationDate/xmp:CreateDate
  mismatch (rule 6.7.3). CI now tests the pinned 26.8.0.3 that the bundles ship.

### DR-53 (proposed, UI): from workstream (d)

1. **`app.css`.** `static/app.css` sits beside the generated `baleen.css`. The CSP (SEC-6) blocks the
   prototype's inline styles, so a few width/utility classes replace them. `baleen.css` stays the
   prototype's `<style>` block, untouched.
2. **SEC-7 and the Baleen folder.** Settings › Data "Baleen folder · Open" opens `data/` today, because
   SEC-7 allows reveal only inside the run roots and `BALEEN_HOME/data`. Proposal: allow `BALEEN_HOME`
   itself.
3. **The "→" glyph.** It falls back to a system font; none of the bundled fontsource subsets contains it.
4. **Endpoints beyond Appendix B.** Preview progress polling, `GET /settings/plan` with unsaved values
   (the summary updates while a stepper is moved), and relative stepper steps.
5. **Extra headers.** X-Frame-Options, CORP and COOP headers beyond SEC-6, and a check that the client
   address is loopback in addition to SEC-4.
6. **Activity labels.** The run page shows the design's per-file activities: verifying JPEG, hashing,
   PDF/A export, transcoding, saving, and so on.

### DR-54 (proposed, documents/e-mail): from workstream (b)

1. **Content routing.** Documents whose content is HTML (an HTML page saved as `.doc` or `.xls`) take the
   HTML route (§6.4 sanitising), with note CONTENT_MISMATCH.
2. **Charset supersets.** Declared big5, gb2312 and iso-8859-1 are decoded as their Windows supersets
   (cp950, gbk, cp1252).
3. **LibreOffice profile hardening (R-05, P8).** A fresh LibreOffice profile does fetch remote images
   linked from DOCX, Word 97 `.doc` and HTML, and remote HTML stylesheets. Each Baleen profile therefore
   sets `BlockUntrustedRefererLinks`, a dead HTTP(S) proxy (127.0.0.1:9) and OpenCL/OpenGL off (DR-37).
   This stops all of them, and OpenCL off also saves about 0.8 s per start. Proposal: make these
   settings part of the spec.
4. **Attachments without a name.** Inline non-text parts without a filename count as attachments
   (`attachment-<n><ext>`), so nothing in an e-mail is silently lost.
5. **Password detection.** DOC, PPT, ODF and WPD encryption is detected at plan time (FAILED
   PASSWORD_PROTECTED). Encrypted XLS/OOXML get one LibreOffice attempt first, because Excel's default
   password ("VelvetSweatshop") opens without a prompt.
6. **Damaged OLE files.** These are forced to their own import filter. Otherwise LibreOffice converts them
   as plain text into garbage PDFs (R-12).

### DR-55 (proposed): "System Volume Information" is a system folder

- **Decision.** Add Windows' per-volume `System Volume Information` folder to the §5.2 system list
  (IGNORED SYSTEM_FILE, not entered).
- **Why.** It is access-denied at every drive root. Without this rule, a drive root as source would give
  a FAILED row every time. Any other unreadable subfolder is now one FAILED `SOURCE_UNREADABLE` row,
  and the run continues (P6, P9).

### DR-56 (proposed): macOS headless LibreOffice needs a fontconfig file for system fonts

- **Finding.** Found by the macOS CI smoke test (bundle.yml) and a diagnostic run. Headless LibreOffice
  on macOS uses its fontconfig-based backend, which sees only LibreOffice's bundled fonts, never the
  system's. CJK text was therefore drawn with Liberation fonts that have no CJK glyphs, and V-TEXT
  correctly failed it (TEXT_LOSS: 4.5 % of a CJK text file's characters present).
- **Decision.** On macOS, Baleen points `FONTCONFIG_FILE` at a generated `data/fontconfig/fonts.conf`.
  It adds the macOS system font folders and puts the macOS CJK families last as a fallback. Its cache
  stays inside `data/` (AC-11). This keeps DR-15 (system fonts only; no bundled Noto).
- **Verified on a macOS runner (diagnostic run 37347702596).**
  - LibreOffice loads the file, and CJK text is embedded as STHeiti TC (TrueType).
  - V-TEXT and V-PDFA pass at 2b and at 1b for the smoke text, pure CJK text and Big5 text.
  - Because the fallback is TrueType, the PDF/A-1b concern of DR-52 does not arise on macOS with system
    fonts.

### DR-57 (proposed): keep the Media lane from holding the whole processor budget

- **Finding.** §5.5 gives each encode ⌊B/F⌋ tokens with F ≤ 2, so two encodes hold all of B.
  - On the benchmark set (Balanced, B = 14), LibreOffice could not start until both encodes had
    finished (videos 16–50 s, documents 39–109 s).
  - x264 at 480p used about 3.5 cores of its 7 tokens.
  - Gentle, Balanced, Maximum and custom 8 all finished in 105–130 s.
- **Proposal.** Cap the Media lane's total at about ⌊2B/3⌋ tokens, or give each encode
  min(⌊B/F⌋, 8) tokens with F up to 3. Files and documents can then run alongside encodes. To be decided
  by the spec owner; Baleen implements §5.5 as written.

### DR-58 (proposed): tools stay within their tokens (AC-15)

- **Decision.** In the sanitised tool environment:
  - LibreOffice gets `MAX_CONCURRENCY=1` (one token per instance);
  - the veraPDF JVM gets `-XX:ActiveProcessorCount=1 -XX:+UseSerialGC -XX:TieredStopAtLevel=1`.
- **Why.** Without them, custom 1 measured 2.01 cores in its busiest 30 s, against a limit of 1.5. With
  them it measures 1.18.
- **Cost.** A 25-file veraPDF batch takes 7.3 s instead of 3.3 s.

## Risks R-01…R-14: findings on this machine

| Risk | Finding |
|---|---|
| R-01 LibreOffice headless from a portable folder on macOS | **Works on GitHub's macOS runner**: documents, e-mail, text and CJK text, once DR-56 gave headless LibreOffice the system fonts. A real, quarantined Mac is on the manual checklist |
| R-02 veraPDF headless install; spaces/CJK on Windows | Headless IzPack install works (Windows locally, macOS and Linux in CI). Paths with spaces work once JAVA_TOOL_OPTIONS paths are quoted. CJK file names are staged under ASCII names. A Baleen folder whose path is outside the ANSI code page cannot start Java; this is reported honestly (DR-49) |
| R-03 Native folder dialogs | Implemented (PowerShell `-STA` FolderBrowserDialog, `osascript`, zenity/kdialog); command and output parsing unit-tested. Dialog focus, UTF-8 and UNC are manual |
| R-04 SMB specifics | UNC paths via `\\localhost\C$` work end to end (CJK folder, network detected, T = 4). A real NAS (NFD names, slow listings, mapped drives) is manual |
| R-05 LibreOffice fetching remote content | A fresh profile does fetch remote images (DOCX, DOC, HTML) and stylesheets. The Baleen profile (BlockUntrustedRefererLinks, dead proxy) stops HTTP(S): 0 outbound attempts offline, plus a local listener test. **Residual:** UNC links (Known gaps) |
| R-06 Old DOC layout fidelity | Not assessed (needs Word); manual |
| R-07 Big5 e-mails with wrong or missing charset | CHARSET_ERRORS path tested, with a decode hint. **Residual:** Big5 declared as iso-8859-1 decodes silently into wrong characters |
| R-08 GPL source obligations | Resolved by building FFmpeg + x264 from source (DR-48); sources attached to releases |
| R-09 Windows long paths and reserved names | Extended-length paths throughout; 255-unit names and >300-character paths tested without LongPathsEnabled. Reserved names (CON, AUX, …) in source trees are not tested |
| R-10 Very large files | Streaming hashes, raised Pillow pixel limit, >32 MP memory reservation, FFmpeg timeouts. Not tested with multi-GB files |
| R-11 MP3-in-MP4 playback | Manual (QuickTime, WMP) |
| R-12 LibreOffice batch edge cases | A corrupt document inside a batch fails alone and its neighbours convert. Damaged OLE files get their own import filter. Batching is now real (the scheduler bug is fixed) |
| R-13 Parallel transfers on a small NAS | The transfer budget is enforced and tested (peak transfers ≤ T). Behaviour on a real NAS is manual |
| R-14 Priority and keep-awake APIs | Windows verified by tests: BELOW_NORMAL children (NORMAL when off), below-normal task threads, SetThreadExecutionState held and released. macOS QoS, nice and caffeinate are implemented but not verified on a Mac |

## Known gaps

- **UNC links in .doc (R-05, SEC-9).** A picture in a `.doc` linked to `\\host\share\…` still makes
  LibreOffice open an SMB connection. On Windows that can expose NTLM credentials to a hostile file's
  host. Mitigation ideas for v1.0: rewrite external links in a staged copy before conversion, or flag
  such documents for review. The offline AC-10 run is Linux-only.
- **Big5 declared as iso-8859-1 (R-07)** cannot be detected by a strict decode.
- **macOS is verified only on GitHub runners.** It still needs the manual checklist on a real Mac:
  quarantine, launcher, dialogs, QoS/nice/caffeinate, network-volume prompts.
- **AC-14** covers Chromium only. Safari, Firefox and Edge are manual.
- **AC-13 and AC-16** were measured on this 28-thread desktop, not the spec's 2020 laptop or 8-core
  Mac and PC.
- **Defender's effect** on output writes was not isolated, because exclusions are not allowed.
- **UI-R7 with a real disconnect.** A real drive disconnect was not tested; the stopped state was
  simulated. The journal lives in the output's `_baleen/`, so if the drive vanishes the report fallback
  in `data/reports/` can only be built from what is still readable.
- **Startup time.** The bundled LibreOffice (admin install) starts about 1.2 s slower than the identical
  system install on this machine; the cause is unknown.
- **Not tested:** reserved file names (R-09) and multi-GB files (R-10).
- **Housekeeping.**
  - The branches `ffmpeg-build` and `diag-mac` remain on GitHub; deleting branches needs your approval.
  - One empty test folder could not be removed without admin rights:
    `%TEMP%\baleen-test\walking-whale-baleen-168e0097\pytest-leftover-acl\test_unreadable_subfolder_is_o0\src\locked`.
    An earlier version of a test set a deny-(RX) ACL on it; the test now denies listing only and always
    removes its entry. To remove the folder, run as administrator:
    `takeown /f <path> /r /d y`, then `icacls <path> /reset /t`, then delete it.
