"""The Check workflow (UI-K1, UI-K2): verify a folder read-only."""

from __future__ import annotations

from ..model import Mode
from . import Explainer, FolderField, Workflow, register

CHECK = register(Workflow(
    id="check",
    name="Check",
    icon="check",
    order=20,
    kicker="Workflows",
    description="Verify a folder without changing it: every file is decoded or validated and fingerprinted.",
    mode=Mode.CHECK,
    folders=(
        FolderField("check_dir", "Folder to check", "read",
                    "Baleen only reads from it. Nothing is written to this folder.", "/path/to/folder"),
    ),
    options=(),
    preview=False,
    start_label="Start check",
    confirm=False,
    result_columns=("status", "source", "action", "reason"),
    explainer=Explainer(
        "What Baleen checks",
        (
            "JPEG and TIFF images are fully decoded.",
            "PDFs are validated as PDF/A with veraPDF.",
            "MP4 and M4A files are fully decoded.",
            "Every file gets a SHA-256 fingerprint.",
            "Files that could be converted are listed as Not archival yet, with their target format.",
        ),
        "The report is saved in Baleen's data folder and can be downloaded from the run page.",
    ),
    ready_text="Ready to check. Nothing will be written to this folder.",
))
