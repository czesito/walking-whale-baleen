"""E-mail items through the whole engine (P9, §7.3, §7.5, §10.1), LibreOffice and veraPDF simulated (workstream b)."""

from __future__ import annotations

import copy
import hashlib
import io
import os
from email.message import EmailMessage
from pathlib import Path

import pytest

from baleen import settings as S
from baleen.convert import libreoffice
from baleen.convert.base import LoResult
from baleen.home import Home
from baleen.model import CheckResult, CheckState, Mode
from baleen.report import read_csv
from baleen.runner import Engine, RunSpec
from baleen.settings import SettingsStore
from baleen.tools import Toolset

JPEG_COLOR = (10, 120, 200)


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 12), JPEG_COLOR).save(buf, "JPEG")
    return buf.getvalue()


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


CALLS: list[list[str]] = []


def fake_convert_batch(ctx, run, jobs):  # noqa: ANN001, ANN201
    CALLS.append([os.path.basename(j.input_path) for j in jobs])
    out = []
    for j in jobs:
        with open(j.expected_output, "wb") as f:
            f.write(_pdf_bytes())
        out.append(LoResult(j, True, j.expected_output, method=f"LibreOffice 26.8.0 · {j.export_filter} · PDF/A-2b"))
    return out


def fake_verapdf(ctx, run, items):  # noqa: ANN001, ANN201
    return [CheckResult("V-PDFA", CheckState.PASS, flavour) for _path, flavour in items]


@pytest.fixture()
def engine(tmp_path: Path, monkeypatch):  # noqa: ANN001, ANN201
    from baleen.verify import pdf as vpdf

    monkeypatch.setattr(libreoffice, "convert_batch", fake_convert_batch)
    monkeypatch.setattr(vpdf, "verapdf_batch", fake_verapdf)
    CALLS.clear()
    home = Home(tmp_path / "home")
    home.ensure()
    store = SettingsStore(home.settings_path)
    store.update({"processor_use": "custom", "processor_cores": 4, "keep_awake": False})
    tools = Toolset(home)
    monkeypatch.setattr(tools, "missing", lambda: [])  # LibreOffice is simulated
    return Engine(home, store, tools), store


def run(engine, src: Path, out: Path, **workflow):  # noqa: ANN001, ANN201
    eng, store = engine
    st = copy.deepcopy(store.snapshot())
    st["workflow"].update(workflow)
    job = eng.start(RunSpec(Mode.CONVERT, str(src), str(out), st), background=False)
    return {r.source_path: r for r in read_csv(job.report_path())}, job


def mail() -> bytes:
    m = EmailMessage()
    m["From"] = "A <a@example.invalid>"
    m["To"] = "B <b@example.invalid>"
    m["Subject"] = "Photos"
    m["Date"] = "Mon, 02 Oct 2006 09:30:00 +0800"
    m.set_content("See attached.")
    m.add_attachment(_jpeg(), maintype="image", subtype="jpeg", filename="photo.jpg")
    inner = EmailMessage()
    inner["From"] = "C <c@example.invalid>"
    inner["Subject"] = "Inner"
    inner.set_content("inner")
    inner.add_attachment(_jpeg(), maintype="image", subtype="jpeg", filename="photo.jpg")
    m.add_attachment(inner, filename="fwd.eml")
    m.add_attachment(b"PK\x05\x06" + b"\0" * 18, maintype="application", subtype="zip", filename="bundle.zip")
    return bytes(m)


def test_extract_end_to_end(tmp_path: Path, engine) -> None:  # noqa: ANN001
    src, out = tmp_path / "src", tmp_path / "out"
    (src / "box").mkdir(parents=True)
    data = mail()
    (src / "box" / "mail.eml").write_bytes(data)
    rows, job = run(engine, src, out)
    assert list(rows) == ["box/mail.eml", "box/mail.eml#photo.jpg", "box/mail.eml#fwd.eml",
                          "box/mail.eml#fwd.eml#photo.jpg", "box/mail.eml#bundle.zip"]
    assert rows["box/mail.eml"].output_path == "box/mail.pdf" and rows["box/mail.eml"].status == "OK"
    assert rows["box/mail.eml"].method.startswith("Python email → HTML · LibreOffice 26.8.0")
    assert rows["box/mail.eml#photo.jpg"].output_path == "box/mail_attachments/photo.jpg"
    assert rows["box/mail.eml#fwd.eml"].output_path == "box/mail_attachments/fwd.pdf"
    assert rows["box/mail.eml#fwd.eml#photo.jpg"].output_path == "box/mail_attachments/fwd_attachments/photo.jpg"
    z = rows["box/mail.eml#bundle.zip"]
    assert z.status == "UNSUPPORTED" and z.output_path == ""
    assert z.source_sha256 == hashlib.sha256(b"PK\x05\x06" + b"\0" * 18).hexdigest()
    for name, r in rows.items():
        if "#" in name:
            assert r.source_mtime == "" and r.source_sha256 and r.source_size, name
    assert rows["box/mail.eml#photo.jpg"].source_sha256 == hashlib.sha256(_jpeg()).hexdigest()
    assert (out / "box" / "mail_attachments" / "fwd_attachments" / "photo.jpg").read_bytes() == _jpeg()
    # the e-mail PDF names each attachment's saved path relative to the PDF
    html_inputs = [c for call in CALLS for c in call if c.startswith("mail-")]
    assert len(html_inputs) == 2


def test_block_and_list(tmp_path: Path, engine) -> None:  # noqa: ANN001
    src = tmp_path / "src"
    src.mkdir()
    (src / "mail.eml").write_bytes(mail())
    rows, _ = run(engine, src, tmp_path / "o1", eml_attachments="block")
    assert list(rows) == ["mail.eml"]
    assert rows["mail.eml"].status == "NEEDS_REVIEW" and rows["mail.eml"].reason == "EML_ATTACHMENTS_BLOCKED"
    assert not (tmp_path / "o1" / "mail.pdf").exists() and rows["mail.eml"].source_sha256
    rows, _ = run(engine, src, tmp_path / "o2", eml_attachments="list")
    assert list(rows) == ["mail.eml"]
    assert rows["mail.eml"].status == "OK" and rows["mail.eml"].reason == "ATTACHMENTS_DROPPED"
    assert (tmp_path / "o2" / "mail.pdf").exists() and not (tmp_path / "o2" / "mail_attachments").exists()


def test_charset_errors_written_and_kept_on_resume(tmp_path: Path, engine) -> None:  # noqa: ANN001
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "big5.eml").write_bytes(b"From: a@example.invalid\r\nSubject: x\r\nMIME-Version: 1.0\r\n"
                                   b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + "訪談記錄".encode("big5"))
    rows, _ = run(engine, src, out)
    r = rows["big5.eml"]
    assert r.status == "NEEDS_REVIEW" and r.reason == "CHARSET_ERRORS" and r.output_path == "big5.pdf"
    assert "decodes as big5" in r.message and (out / "big5.pdf").exists()
    rows, _ = run(engine, src, out)  # §7.5: re-run resumes, and the flag stays
    r = rows["big5.eml"]
    assert r.status == "NEEDS_REVIEW" and set(r.reason.split(";")) == {"RESUMED", "CHARSET_ERRORS"}
    assert r.output_path == "big5.pdf" and r.output_sha256


def test_unparseable_email_fails_at_plan_time(tmp_path: Path, engine) -> None:  # noqa: ANN001
    src = tmp_path / "src"
    src.mkdir()
    (src / "noise.eml").write_bytes(b"\x00\x01\x02 not an e-mail")
    rows, _ = run(engine, src, tmp_path / "out")
    r = rows["noise.eml"]
    assert r.status == "FAILED" and r.reason == "CONVERSION_ERROR" and r.action == "none" and r.method == ""
    assert CALLS == []


def test_documents_settings_never_change_names(tmp_path: Path, engine) -> None:  # noqa: ANN001
    """§7.3: attachment names are the same under every policy and with hide_email_addresses (P7)."""
    from baleen.convert.base import ProbeContext
    from baleen.model import ScanEntry
    from baleen.plan import Planner

    src = tmp_path / "src"
    src.mkdir()
    (src / "mail.eml").write_bytes(mail())
    names = []
    for opts in ({}, {"eml_attachments": "block"}, {"eml_attachments": "list"}, {"hide_email_addresses": True}):
        st = copy.deepcopy(S.defaults())
        st["workflow"].update(opts)
        home = Home(tmp_path / "h")
        plan = Planner(ProbeContext(Mode.CONVERT, st, Toolset(home), home), str(src), str(tmp_path / "o")).build(
            [ScanEntry("mail.eml", (src / "mail.eml").stat().st_size, 0)])
        # The e-mail and its direct attachments. (Under block/list a nested e-mail that is itself
        # decided at plan time is not expanded further; nothing below it is materialised then.)
        names.append([(it.source_path, it.output_path) for it in plan.items if it.source_path.count("#") <= 1])
    assert all(n == names[0] for n in names), names
