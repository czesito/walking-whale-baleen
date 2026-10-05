"""E-mail PDFs (§6.5) checked by their content, with the real LibreOffice (workstream b).

- hide_email_addresses: no address in the PDF text, link annotations or metadata; display names stay.
- cid: images and remote tracking pixels appear as placeholder text; Big5 text survives.
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

from baleen.home import Home
from baleen.model import Mode
from baleen.report import read_csv
from baleen.runner import Engine, RunSpec
from baleen.settings import SettingsStore
from baleen.tools import Toolset

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.groups import email as fx  # noqa: E402

pytestmark = pytest.mark.integration
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    photo = fx._jpeg_bytes(tmp_path, "logo.jpg", (200, 40, 40))
    for name, msg in (("plain.eml", fx.plain()), ("html_cid.eml", fx.html_cid(photo)), ("tracking.eml", fx.tracking())):
        fx._save(src, name, msg)
    (src / "big5.eml").write_bytes(fx.big5_messages()["big5.eml"])
    return src


def _convert(src: Path, out: Path, home_dir: Path, **workflow: object) -> dict:
    home = Home(home_dir)
    home.ensure()
    if Toolset(home).path("libreoffice") is None:
        pytest.skip("LibreOffice not available")
    store = SettingsStore(home.settings_path)
    st = copy.deepcopy(store.snapshot())
    st["workflow"].update(workflow)
    st["app"]["keep_awake"] = False
    job = Engine(home, store, Toolset(home)).start(RunSpec(Mode.CONVERT, str(src), str(out), st), background=False)
    return {r.source_path: r for r in read_csv(job.report_path())}


def _pdf(path: Path) -> tuple[str, list[str], str]:
    from pypdf import PdfReader

    r = PdfReader(str(path))
    text = "\n".join(p.extract_text() for p in r.pages)
    uris = []
    for page in r.pages:
        for a in page.get("/Annots") or []:
            action = a.get_object().get("/A")
            if action is not None and action.get_object().get("/URI"):
                uris.append(str(action.get_object()["/URI"]))
    meta = " ".join(str(v) for v in (r.metadata or {}).values())
    xmp = r.xmp_metadata
    if xmp is not None:
        meta += " " + xmp.stream.get_data().decode("utf-8", "replace")
    return text, uris, meta


def test_hidden_addresses_and_placeholders(tmp_path: Path) -> None:
    src = _source(tmp_path)
    rows = _convert(src, tmp_path / "out", tmp_path / "home", hide_email_addresses=True)
    for name in ("plain.eml", "html_cid.eml", "tracking.eml", "big5.eml"):
        assert rows[name].output_path, (name, rows[name].reason, rows[name].message)
    for name in ("plain", "html_cid", "tracking", "big5"):
        text, uris, meta = _pdf(tmp_path / "out" / f"{name}.pdf")
        flat = text.replace("\n", " ")
        assert not ADDRESS.search(flat), (name, ADDRESS.findall(flat))
        assert not any(ADDRESS.search(u.replace("%40", "@")) for u in uris), (name, uris)
        assert not ADDRESS.search(meta), (name, ADDRESS.findall(meta))
        assert "[address hidden]" in flat and "Test Sender" in flat
    text, uris, _ = _pdf(tmp_path / "out" / "html_cid.pdf")
    assert "[inline image: logo.jpg, saved as attachment]" in text.replace("\n", "")
    assert any(u.startswith("mailto:") and "address" in u for u in uris), uris  # LibreOffice may %-encode it
    text, _, _ = _pdf(tmp_path / "out" / "tracking.pdf")
    flat = text.replace("\n", "")
    assert "[external resource not archived: http://tracker.example.invalid/open.gif?id=8f3a]" in flat
    assert "[external resource not archived: https://cdn.example.invalid/bg.png]" in flat
    text, _, _ = _pdf(tmp_path / "out" / "big5.pdf")
    assert "訪談記錄" in text and "�" not in text
    assert rows["html_cid.eml#logo.jpg"].output_path == "html_cid_attachments/logo.jpg"


def test_addresses_shown_by_default(tmp_path: Path) -> None:
    src = _source(tmp_path)
    _convert(src, tmp_path / "out", tmp_path / "home")
    text, _, _ = _pdf(tmp_path / "out" / "plain.pdf")
    flat = text.replace("\n", " ")
    assert "sender@example.invalid" in flat and "curator@example.invalid" in flat
    assert "[address hidden]" not in flat
    assert "(2006-10-02T09:30:00+08:00)" in flat
