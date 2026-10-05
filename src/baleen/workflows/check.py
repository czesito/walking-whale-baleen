"""The Check workflow (UI-K1, UI-K2): verify a folder read-only."""

from __future__ import annotations

from ..model import Mode
from . import Explainer, FolderField, Workflow, register

CHECK = register(Workflow(
    id="check",
    name="Check",
    icon="check",
    order=20,
    kicker="Workflow",
    description="Verify a folder without changing it. Find files that aren't archival yet, and archival files "
                "that are damaged.",
    mode=Mode.CHECK,
    folders=(
        FolderField("check_dir", "Folder to check", "read",
                    "Any folder, including a Baleen output folder. Nothing in it is changed."),
    ),
    options=(),
    preview=False,
    start_label="Start check",
    confirm=False,
    result_columns=("status", "source", "action", "reason"),
    explainer=Explainer(
        "What Baleen checks",
        (
            "JPEG and TIFF · decode every frame fully",
            "PDF · PDF/A validation with veraPDF",
            "MP4 and M4A · probe, then decode the whole file",
            "Every file · SHA-256 recorded in the report",
            "Everything else · listed with the archival format it would convert to",
        ),
        icons=("cat-image", "cat-pdf", "cat-video", "shield", "cat-other"),
    ),
    ready_text="Ready to check. Nothing will be written to this folder.",
    extra={
        "running_label": "Checking",
        "idle_text": "Choose a folder to check.",
        "busy_text": "Wait for the current run to finish.",
    },
))
