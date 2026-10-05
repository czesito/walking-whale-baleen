"""The Convert workflow (UI-C1…C6). Copy from design §02 and the prototype."""

from __future__ import annotations

from ..model import Mode
from . import FolderField, OptionField, OptionGroup, Workflow, register

SCHEMA: tuple[OptionGroup, ...] = (
    OptionGroup("Documents", (
        OptionField("pdfa_level", "PDF/A level", "For new PDFs. 2b suits most archives.", "seg",
                    (("1b", "1b"), ("2b", "2b"), ("3b", "3b")), "2b"),
        OptionField("txt_encoding", "Text encoding when unsure",
                    "Used only when a text file isn't clearly UTF-8. Auto flags it for review instead of guessing.",
                    "select",
                    (("auto", "Auto (flag for review)"), ("big5", "Big5 (Traditional Chinese)"),
                     ("gb18030", "GB18030 (Simplified Chinese)"), ("shift_jis", "Shift JIS (Japanese)"),
                     ("windows-1252", "Windows-1252 (Western)")), "auto"),
    )),
    OptionGroup("E-mail", (
        OptionField("eml_attachments", "Attachments",
                    "Extract saves and converts every attachment next to the e-mail's PDF.", "seg",
                    (("extract", "Extract"), ("block", "Block"), ("list", "List only")), "extract"),
        OptionField("hide_email_addresses", "Hide e-mail addresses in PDFs",
                    "For copies you plan to share. Display names are kept.", "switch", (), False),
    )),
    OptionGroup("Audio & video", (
        OptionField("video_quality", "Video quality when re-encoding",
                    "High keeps more detail (CRF 18). Files that are already H.264 are never re-encoded.", "seg",
                    (("high", "High"), ("standard", "Standard")), "high"),
        OptionField("audio_container", "Audio-only output",
                    "Same MPEG-4 format either way; .m4a tells players it's audio.", "seg",
                    (("m4a", ".m4a"), ("mp4", ".mp4")), "m4a"),
    )),
    OptionGroup("Files already archival", (
        OptionField("copy_existing", "Also copy them to the output folder",
                    "On: the output folder becomes the complete, verified set. Off: they're checked in place only.",
                    "switch", (), True),
    )),
)

CONVERT = register(Workflow(
    id="convert",
    name="Convert",
    icon="convert",
    order=10,
    kicker="Workflow",
    description="Turn legacy files into archival formats: JPEG, TIFF, PDF/A, MP4 and M4A. Every result is verified.",
    mode=Mode.CONVERT,
    folders=(
        FolderField("source_dir", "Source folder", "read",
                    "The folder to convert. Baleen only reads from it."),
        FolderField("output_dir", "Output folder", "write",
                    "Converted files and the report go here, in the same sub-folder structure. "
                    "Must be outside the source folder."),
    ),
    options=SCHEMA,
    preview=True,
    start_label="Start converting",
    confirm=True,
    result_columns=("status", "source", "output", "action", "reason"),
    explainer=None,
    ready_text="Ready. A preview is optional but recommended.",
    reassurance="Your source folder is never changed.",
    extra={
        "running_label": "Converting",
        "idle_text": "Choose a source and an output folder.",
        "busy_text": "Wait for the current run to finish before starting another.",
        "confirm_text": "Baleen will convert and verify every file, then write a report.",
        # Settings chips on the confirmation and the run page. "{value}" is the stored value,
        # "{onoff}" is on/off; a switch without a placeholder shows only when it is on.
        "chips": (
            ("pdfa_level", "PDF/A-{value}"),
            ("txt_encoding", "Text: {value}"),
            ("eml_attachments", "Attachments: {value}"),
            ("video_quality", "Video: {value}"),
            ("audio_container", "Audio: .{value}"),
            ("copy_existing", "Copy archival: {onoff}"),
            ("hide_email_addresses", "Addresses hidden"),
        ),
    },
))
