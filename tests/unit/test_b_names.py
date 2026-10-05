"""§7.8 sanitising attachment names (workstream b)."""

from __future__ import annotations

import pytest

from baleen.convert.names import MAX_NAME_BYTES, sanitise_attachment_name


@pytest.mark.parametrize(("raw", "expected"), [
    ("photo.jpg", "photo.jpg"),
    ("訪談記錄.doc", "訪談記錄.doc"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\me\\Desktop\\report.doc", "report.doc"),
    ("dir/sub\\name.txt", "name.txt"),
    ("bell\x07and\x1bescape\x7f.txt", "bellandescape.txt"),
    ("tab\there.txt", "tabhere.txt"),
    ('a<b>c:d"e|f?g*h.pdf', "a_b_c_d_e_f_g_h.pdf"),
    ("trailing dots...", "trailing dots"),
    ("trailing space .doc  ", "trailing space .doc"),
    ("CON", "CON_"),
    ("con.txt", "con_.txt"),
    ("Lpt9.tar.gz", "Lpt9_.tar.gz"),
    ("COM1 .txt", "COM1 _.txt"),
    ("CONSOLE.txt", "CONSOLE.txt"),
    ("COM10.txt", "COM10.txt"),
])
def test_rules(raw: str, expected: str) -> None:
    assert sanitise_attachment_name(raw, 1) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "...", "/", "\\\\", "\x00\x01", ". . ."])
def test_empty_result_becomes_attachment_n(raw: str | None) -> None:
    assert sanitise_attachment_name(raw, 3) == "attachment-3"
    assert sanitise_attachment_name(raw, 3, ".jpg") == "attachment-3.jpg"


def test_truncated_to_200_bytes_keeping_extension() -> None:
    name = "訪" * 100 + ".docx"  # 300 bytes of stem
    out = sanitise_attachment_name(name, 1)
    assert out.endswith(".docx")
    assert len(out.encode("utf-8")) <= MAX_NAME_BYTES
    assert out == "訪" * 65 + ".docx"  # 195 + 5 bytes, no split character
    ascii_name = "x" * 250 + ".pdf"
    assert sanitise_attachment_name(ascii_name, 1) == "x" * 196 + ".pdf"


def test_truncation_never_leaves_trailing_dots() -> None:
    name = "a" * 195 + "....." + "b" * 30
    out = sanitise_attachment_name(name, 1)
    assert not out.endswith((".", " ")) and len(out.encode()) <= MAX_NAME_BYTES


def test_lone_surrogates_are_replaced() -> None:
    assert sanitise_attachment_name("bad\udcb3name.doc", 1) == "bad\ufffdname.doc"


def test_deterministic() -> None:
    raw = "../a:b/?c*.txt"
    assert {sanitise_attachment_name(raw, 2) for _ in range(5)} == {"_c_.txt"}
