"""Settings profiles used by expected.csv. 'check' runs the Check workflow (no output)."""

PROFILES: dict[str, dict[str, object]] = {
    "default": {},
    "check": {},
    "big5": {"txt_encoding": "big5"},
    "eml_block": {"eml_attachments": "block"},
    "eml_list": {"eml_attachments": "list"},
    "hide_addr": {"hide_email_addresses": True},
    "audio_mp4": {"audio_container": "mp4"},
    "video_standard": {"video_quality": "standard"},
    "pdfa_1b": {"pdfa_level": "1b"},
    "pdfa_3b": {"pdfa_level": "3b"},
    "copy_off": {"copy_existing": False},
}
