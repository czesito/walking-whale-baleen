# Baleen

**by Walking Whale**

Turn legacy archive files into archival formats, and prove that every one of them is sound.

Baleen walks a folder tree and converts every non-archival file into one of five archival
formats (JPEG, TIFF, PDF/A, MP4, M4A). Every output, and every file that was already
archival, is verified (full decode, veraPDF PDF/A validation, full media decode) and
recorded with SHA-256 in a CSV report. The source folder is never modified.

It runs from a portable folder on macOS (Apple silicon) and Windows: double-click
*Start Baleen*, and delete the folder to uninstall. No installer, no network.

## Documents

- [Specification v1.2](docs/baleen-spec.html): behaviour, rules and stable IDs.
- [Design v1.1](docs/baleen-design.html): information architecture, tokens, wireframes.
- [Clickable prototype](docs/prototype/baleen-prototype.html).

## Status

Work in progress towards v1. See `STATUS.md`.

## Licence

Apache-2.0, copyright 2026 Walking Whale Co., Ltd. See `LICENSE` and `NOTICE`.

The Baleen and Walking Whale names and logos are trademarks of Walking Whale Co., Ltd.
and are not licensed under Apache-2.0. Logo files live in `assets/brand/`.

Please never attach archive material to issues; reports and logs may contain personal
names in paths.
