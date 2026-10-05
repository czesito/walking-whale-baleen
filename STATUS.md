# Baleen: implementation status

Status of the v1 implementation against Specification v1.2 and Design v1.1.
This file is updated as work lands. Sections marked *(pending)* are filled in at the end of the session.

## What works and how to run it

*(pending)*

## Spec ID coverage

*(pending)*

## Benchmarks

*(pending)*

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

## Risks R-01…R-14: findings on this machine

*(pending)*

## Known gaps

*(pending)*
