"""§7 naming: clash rule (DR-05), determinism (§7.4, P7), attachments, PATH_TOO_LONG."""

from __future__ import annotations

import copy
import random

import pytest

from baleen import settings as S
from baleen.convert import base
from baleen.convert.base import ChildSpec, ProbeContext, Route, SourceRef
from baleen.home import Home
from baleen.model import Action, Category, Mode, Probe, ScanEntry
from baleen.plan import Planner, split_name
from baleen.tools import Toolset


class NameOnly(Route):
    """Probe from the extension only (the target rules of §6), no content needed."""

    def __init__(self, key: str) -> None:
        self._key = key

    @property
    def key(self) -> str:  # type: ignore[override]
        return self._key

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        from baleen.convert.formats import default_target, is_archival_ext, lookup

        f = lookup(src.ext)
        act = Action.COPY if is_archival_ext(src.ext) else Action.CONVERT
        return Probe(f.category if f else Category.OTHER, default_target(src.ext, ctx.settings), act,
                     route=self._key)


class FakeEmail(NameOnly):
    """Attachments are declared in the file body: lines 'ATTACH:<name>'."""

    def expand(self, ctx, item, src):  # noqa: ANN001, ANN201
        out = []
        for line in src.read_head().decode().splitlines():
            if line.startswith("ATTACH:"):
                name = line[7:]
                body = b"nested\n" if name.endswith(".eml") else b"x"
                out.append(ChildSpec(name=name, data=body, depth=item.depth + 1))
        return out


@pytest.fixture(autouse=True)
def routes(monkeypatch) -> None:  # noqa: ANN001
    reg = {k: NameOnly(k) for k in ("image", "document", "text", "html", "pdf", "media")}
    reg["email"] = FakeEmail("email")
    monkeypatch.setattr(base, "_registry", reg)
    monkeypatch.setattr(base, "_loaded", True)


def plan_for(tmp_path, names: dict[str, bytes], mode=Mode.CONVERT, shuffle_seed=None, **opts):  # noqa: ANN001, ANN201
    src = tmp_path / "src"
    for rel, data in names.items():
        p = src.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    entries = [ScanEntry(rel, len(d), 0) for rel, d in names.items()]
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(entries)
    st = copy.deepcopy(S.defaults())
    st["workflow"].update(opts)
    home = Home(tmp_path / "home")
    ctx = ProbeContext(mode, st, Toolset(home), home)
    return Planner(ctx, str(src), str(tmp_path / "out"), workers=4).build(entries)


def outputs(plan) -> dict[str, str | None]:  # noqa: ANN001
    return {it.source_path: it.output_path for it in plan.items}


def test_split_name() -> None:
    assert split_name("report.doc") == ("report", ".doc")
    assert split_name(".hidden") == (".hidden", "")
    assert split_name("archive.tar.gz") == ("archive.tar", ".gz")
    assert split_name("noext") == ("noext", "")


@pytest.mark.parametrize("files, expected", [
    ({"report.doc": b""}, {"report.doc": "report.pdf"}),
    ({"IMG_0001.JPG": b""}, {"IMG_0001.JPG": "IMG_0001.jpg"}),
    ({"訪談.doc": b"", "訪談.eml": b""}, {"訪談.doc": "訪談_doc.pdf", "訪談.eml": "訪談_eml.pdf"}),
    ({"photo.bmp": b"", "photo.jpg": b""}, {"photo.bmp": "photo_bmp.jpg", "photo.jpg": "photo_jpg.jpg"}),
    ({"notes.txt": b"", "notes.pdf": b""}, {"notes.txt": "notes_txt.pdf", "notes.pdf": "notes_pdf.pdf"}),
])
def test_clash_table(tmp_path, files, expected) -> None:  # noqa: ANN001
    assert {k: v for k, v in outputs(plan_for(tmp_path, files)).items() if k in expected} == expected


def test_case_only_clash_unresolved(tmp_path) -> None:  # noqa: ANN001
    # On a case-insensitive volume both names cannot exist; build the plan from a listing.
    from baleen.model import ScanEntry

    st = copy.deepcopy(S.defaults())
    home = Home(tmp_path / "home")
    (tmp_path / "src").mkdir()
    ctx = ProbeContext(Mode.CONVERT, st, Toolset(home), home)
    entries = [ScanEntry("a.JPG", 1, 0), ScanEntry("a.jpg", 1, 0)]
    plan = Planner(ctx, str(tmp_path / "src"), str(tmp_path / "out")).build(entries)
    for it in plan.items:
        assert it.reasons == ["NAME_CLASH_UNRESOLVED"]
        assert it.output_path == "a_jpg.jpg"


def test_shuffle_does_not_change_names(tmp_path) -> None:  # noqa: ANN001
    files = {f"d{i % 3}/f{i}.{ext}": b"" for i, ext in enumerate(
        ["doc", "jpg", "bmp", "txt", "pdf", "eml", "avi", "wma", "png", "JPG"] * 4)}
    files.update({"d0/same.doc": b"", "d0/same.rtf": b"", "d0/SAME.txt": b""})
    base_plan = outputs(plan_for(tmp_path / "a", files))
    for seed in range(5):
        assert outputs(plan_for(tmp_path / f"s{seed}", files, shuffle_seed=seed)) == base_plan


def test_copy_existing_does_not_change_names(tmp_path) -> None:  # noqa: ANN001
    files = {"photo.jpg": b"", "photo.bmp": b"", "x.pdf": b"", "x.doc": b""}
    a = outputs(plan_for(tmp_path / "a", files, copy_existing=True))
    b = outputs(plan_for(tmp_path / "b", files, copy_existing=False))
    assert a == b


def test_audio_container_changes_extension(tmp_path) -> None:  # noqa: ANN001
    assert outputs(plan_for(tmp_path / "a", {"song.wma": b""}))["song.wma"] == "song.m4a"
    assert outputs(plan_for(tmp_path / "b", {"song.wma": b""}, audio_container="mp4"))["song.wma"] == "song.mp4"


def test_attachments_planned_after_their_email(tmp_path) -> None:  # noqa: ANN001
    files = {"訪談.eml": b"ATTACH:photo.jpg\nATTACH:photo.bmp\nATTACH:fwd.eml\n", "訪談.doc": b"", "z.jpg": b""}
    plan = plan_for(tmp_path, files)
    paths = [it.source_path for it in plan.items]
    i = paths.index("訪談.eml")
    assert paths[i + 1:i + 4] == ["訪談.eml#photo.jpg", "訪談.eml#photo.bmp", "訪談.eml#fwd.eml"]
    out = outputs(plan)
    assert out["訪談.eml"] == "訪談_eml.pdf"
    assert out["訪談.eml#photo.jpg"] == "訪談_eml_attachments/photo_jpg.jpg"
    assert out["訪談.eml#photo.bmp"] == "訪談_eml_attachments/photo_bmp.jpg"
    assert out["訪談.eml#fwd.eml"] == "訪談_eml_attachments/fwd.pdf"
    kids = [it for it in plan.items if it.parent == plan.items[i].n]
    assert len(kids) == 3


def test_attachments_block_policy_keeps_names_but_no_rows(tmp_path) -> None:  # noqa: ANN001
    files = {"m.eml": b"ATTACH:a.jpg\n"}
    plan = plan_for(tmp_path, files, eml_attachments="block")
    kid = [it for it in plan.items if it.parent][0]
    assert not kid.materialise and kid.output_path == "m_attachments/a.jpg"


def test_check_mode_marks_convertible(tmp_path) -> None:  # noqa: ANN001
    plan = plan_for(tmp_path, {"a.bmp": b"", "b.jpg": b"", "c.eml": b"ATTACH:x.jpg\n"}, mode=Mode.CHECK)
    by = {it.source_path: it for it in plan.items}
    assert by["a.bmp"].reasons == ["NOT_ARCHIVAL_FORMAT"] and "JPEG" in by["a.bmp"].message
    assert by["b.jpg"].action == Action.CHECK and not by["b.jpg"].reasons
    assert all(it.output_path is None for it in plan.items)
    assert "c.eml#x.jpg" not in by  # attachments are not extracted in Check mode


def test_unsupported_and_ignored(tmp_path) -> None:  # noqa: ANN001
    plan = plan_for(tmp_path, {"a.zip": b"", "b.msg": b"", "README": b""})
    for it in plan.items:
        assert it.reasons == ["UNSUPPORTED_FORMAT"] and it.output_path is None


def test_path_too_long(tmp_path) -> None:  # noqa: ANN001
    st = copy.deepcopy(S.defaults())
    home = Home(tmp_path / "home")
    (tmp_path / "src").mkdir()
    ctx = ProbeContext(Mode.CONVERT, st, Toolset(home), home)
    long_stem = "x" * 250
    entries = [ScanEntry(f"{long_stem}.bmp", 1, 0), ScanEntry(f"{long_stem}.jpg", 1, 0)]
    plan = Planner(ctx, str(tmp_path / "src"), str(tmp_path / "out")).build(entries)
    for it in plan.items:
        assert "PATH_TOO_LONG" in it.reasons  # x*250 + "_bmp.jpg" = 258 code units


def test_written_reasons_do_not_decide_at_plan_time(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    class Charset(NameOnly):
        def probe(self, ctx, src):  # noqa: ANN001, ANN202
            p = super().probe(ctx, src)
            p.reasons = ["CHARSET_ERRORS"]
            return p

    reg = dict(base._registry)
    reg["text"] = Charset("text")
    monkeypatch.setattr(base, "_registry", reg)
    plan = plan_for(tmp_path, {"a.txt": b"x", "b.zip": b""})
    by = {it.source_path: it for it in plan.items}
    assert not by["a.txt"].final and by["a.txt"].reasons == ["CHARSET_ERRORS"]
    assert by["b.zip"].final


def test_tool_missing_nested_emails_still_release_attachments(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Without LibreOffice, e-mails are TOOL_MISSING, but their attachments (at any depth) still run."""
    from baleen.tools import Toolset as TS

    class NestedEmail(FakeEmail):
        converter_tools = ("libreoffice",)

        def expand(self, ctx, item, src):  # noqa: ANN001, ANN202
            if item.depth == 0:
                return [ChildSpec(name="fwd.eml", data=b"ATTACH:photo.jpg\n", depth=1)]
            return [ChildSpec(name="photo.jpg", data=b"x", depth=item.depth + 1)]

    reg = dict(base._registry)
    reg["email"] = NestedEmail("email")
    monkeypatch.setattr(base, "_registry", reg)
    monkeypatch.setattr(TS, "missing", lambda self: ["libreoffice"])
    plan = plan_for(tmp_path, {"m.eml": b"top"})
    by = {it.source_path: it for it in plan.items}
    assert by["m.eml"].reasons == ["TOOL_MISSING"] and by["m.eml#fwd.eml"].reasons == ["TOOL_MISSING"]
    photo = by["m.eml#fwd.eml#photo.jpg"]
    assert photo.materialise and not photo.final and photo.output_path == "m_attachments/fwd_attachments/photo.jpg"
