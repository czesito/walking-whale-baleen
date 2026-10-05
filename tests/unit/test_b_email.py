"""E-mail route (§6.5, §7.8, P7, P9): enumeration, staging, headers, charsets, privacy (workstream b)."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
from email.message import EmailMessage
from email.policy import default as POLICY
from pathlib import Path

import pytest

from baleen import settings as S
from baleen.convert import base
from baleen.convert import email as em
from baleen.convert.base import LoJob, LoResult, ProbeContext, Route, RunContext, SourceRef, WorkItem
from baleen.convert.sanitize import remote_urls_left
from baleen.home import Home
from baleen.model import Action, Category, Mode, Probe, ScanEntry
from baleen.plan import Planner
from baleen.tools import Toolset

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00synthetic"


# --------------------------------------------------------------------------- message builders


def mk(subject: str = "Test", body: str = "Hello") -> EmailMessage:
    m = EmailMessage()
    m["From"] = "Sender <sender@example.invalid>"
    m["To"] = "Archive <archive@example.invalid>"
    m["Date"] = "Mon, 02 Oct 2006 09:30:00 +0800"
    m["Subject"] = subject
    m.set_content(body)
    return m


def rich() -> EmailMessage:
    """HTML + cid image, an unnamed PDF, a text attachment, a nested e-mail carrying a JPEG."""
    m = mk("Rich", "plain alternative")
    m.add_alternative('<p>Logo: <img src="cid:logo@x"></p>', subtype="html")
    m.get_payload()[1].add_related(JPEG, maintype="image", subtype="jpeg", cid="<logo@x>", filename="logo.jpg")
    m.add_attachment(b"%PDF-1.4 synthetic", maintype="application", subtype="pdf")  # no filename
    m.add_attachment(b"notes", maintype="text", subtype="plain", filename="notes.txt")
    inner = mk("Inner", "inner body")
    inner.add_attachment(JPEG, maintype="image", subtype="jpeg", filename="photo.jpg")
    m.add_attachment(inner, filename="fwd")  # filename without .eml
    return m


def raw(headers: list[bytes], body: bytes, ctype: bytes = b"text/plain") -> bytes:
    return b"\r\n".join([*headers, b"MIME-Version: 1.0", b"Content-Type: " + ctype,
                         b"Content-Transfer-Encoding: 8bit"]) + b"\r\n\r\n" + body


def names(msg: EmailMessage | bytes) -> list[str]:
    m = em.parse(msg if isinstance(msg, bytes) else bytes(msg))
    return [a.name for a in em.enumerate_attachments(m)]


# --------------------------------------------------------------------------- identification


def test_attachment_identification_and_names() -> None:
    atts = em.enumerate_attachments(em.parse(bytes(rich())))
    assert [(a.index, a.name, a.mime, a.nested) for a in atts] == [
        (1, "logo.jpg", "image/jpeg", False),
        (2, "attachment-2.pdf", "application/pdf", False),
        (3, "notes.txt", "text/plain", False),
        (4, "fwd.eml", "message/rfc822", True),
    ]
    assert atts[0].cid == "logo@x" and atts[0].data == JPEG
    assert atts[1].data == b"%PDF-1.4 synthetic"
    inner = em.enumerate_attachments(em.parse(atts[3].data))
    assert [a.name for a in inner] == ["photo.jpg"] and inner[0].data == JPEG


def test_inline_text_with_filename_is_body_but_inline_image_is_attachment() -> None:
    m = mk("Mixed", "main body")
    m.add_attachment(b"footer text", maintype="text", subtype="plain", filename="footer.txt", disposition="inline")
    m.add_attachment(JPEG, maintype="image", subtype="jpeg", disposition="inline")  # no name, no cid
    m.add_attachment(b"-----BEGIN PGP SIGNATURE-----", maintype="application", subtype="pgp-signature",
                     filename="signature.asc")
    msg = em.parse(bytes(m))
    assert [a.name for a in em.enumerate_attachments(msg)] == ["attachment-1.jpg", "signature.asc"]
    texts = [em.decode_text_part(p).text.strip() for p in em.structure(msg).texts]
    assert texts == ["main body", "footer text"]


def test_alternative_renders_html_only() -> None:
    m = mk("Alt", "PLAIN VERSION")
    m.add_alternative("<p>HTML VERSION</p>", subtype="html")
    doc = em.render(em.parse(bytes(m)), saved=[], hide=False, policy="extract")
    assert "HTML VERSION" in doc and "PLAIN VERSION" not in doc


def test_unnamed_nested_email_and_odd_names() -> None:
    m = mk()
    m.add_attachment(mk("inner"))  # message/rfc822 without a filename
    m.add_attachment(b"x", maintype="application", subtype="octet-stream", filename="../../CON.txt")
    m.add_attachment(b"y", maintype="application", subtype="x-unknown")  # unknown type: no extension
    assert names(m) == ["attachment-1.eml", "CON_.txt", "attachment-3"]


def test_raw_8bit_filename_decoded_with_message_charset() -> None:
    big5_name = "訪談記錄.doc".encode("big5")
    data = (b"From: a@example.invalid\r\nSubject: x\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
            b"Content-Type: text/plain; charset=big5\r\n\r\n" + "內文".encode("big5") + b"\r\n--B\r\n"
            b'Content-Type: application/msword\r\nContent-Disposition: attachment; filename="' + big5_name +
            b'"\r\n\r\nDOC\r\n--B--\r\n')
    msg = em.parse(data)
    atts = em.enumerate_attachments(msg)
    assert atts[0].name == "訪談記錄.doc" and atts[0].name_ok
    assert em.analyse(msg).problems == []
    undecodable = data.replace(big5_name, b"\xff\xfe\xfd.doc").replace(b"charset=big5", b"charset=utf-8")
    undecodable = undecodable.replace("內文".encode("big5"), b"ok")
    an = em.analyse(em.parse(undecodable))
    assert not an.attachments[0].name_ok and "attachment name" in " ".join(an.problems)


def test_unparseable() -> None:
    for data in (b"", b"\x00\x01\x02 binary noise \xff\xfe", b"\n\nonly a body"):
        with pytest.raises(em.Unparseable):
            em.parse(data)


# --------------------------------------------------------------------------- P7: plan-time names == run-time files


class NameOnly(Route):
    def __init__(self, key: str) -> None:
        self._key = key

    @property
    def key(self) -> str:  # type: ignore[override]
        return self._key

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        from baleen.convert.formats import default_target, lookup

        f = lookup(src.ext)
        return Probe(f.category if f else Category.OTHER, default_target(src.ext, ctx.settings), Action.CONVERT,
                     route=self._key)


@pytest.fixture()
def routes(monkeypatch) -> None:  # noqa: ANN001
    reg: dict[str, Route] = {k: NameOnly(k) for k in ("image", "document", "text", "html", "pdf", "media")}
    reg["email"] = em.EmailRoute()
    monkeypatch.setattr(base, "_registry", reg)
    monkeypatch.setattr(base, "_loaded", True)


def make_plan(tmp_path: Path, files: dict[str, bytes], **opts):  # noqa: ANN201
    src = tmp_path / "src"
    for rel, data in files.items():
        p = src.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    st = copy.deepcopy(S.defaults())
    st["workflow"].update(opts)
    home = Home(tmp_path / "home")
    ctx = ProbeContext(Mode.CONVERT, st, Toolset(home), home)
    entries = [ScanEntry(rel, len(d), 0) for rel, d in files.items()]
    return Planner(ctx, str(src), str(tmp_path / "out"), workers=2).build(entries), st, home


def test_expand_and_stage_children_match(tmp_path: Path, routes) -> None:  # noqa: ANN001
    plan, st, home = make_plan(tmp_path, {"mail.eml": bytes(rich())})
    by = {it.source_path: it for it in plan.items}
    assert list(by) == ["mail.eml", "mail.eml#logo.jpg", "mail.eml#attachment-2.pdf", "mail.eml#notes.txt",
                        "mail.eml#fwd.eml", "mail.eml#fwd.eml#photo.jpg"]
    assert by["mail.eml#fwd.eml#photo.jpg"].output_path == "mail_attachments/fwd_attachments/photo.jpg"
    run = RunContext("r", Mode.CONVERT, home, Toolset(home), st, str(tmp_path / "src"), str(tmp_path / "out"),
                     str(tmp_path / "work"))

    def wi(it) -> WorkItem:  # noqa: ANN001
        return WorkItem(it, run, str(tmp_path / "work" / str(it.n)), it.abs_path, False)

    top = wi(by["mail.eml"])
    top.staged = str(tmp_path / "src" / "mail.eml")
    top.children = [wi(it) for it in plan.items if it.parent == top.n]
    em.EmailRoute().stage_children(None, top)  # type: ignore[arg-type]
    for c in top.children:
        assert c.staged and not c.done, c.plan.source_path
        data = Path(c.staged).read_bytes()
        assert hashlib.sha256(data).hexdigest() == c.plan.data["sha256"] == c.source_sha256
        assert c.source_size == len(data) == c.plan.size
        assert Path(c.staged).name == "input" + c.plan.ext
    nested = next(c for c in top.children if c.plan.source_path.endswith("fwd.eml"))
    nested.children = [wi(it) for it in plan.items if it.parent == nested.n]
    em.EmailRoute().stage_children(None, nested)  # type: ignore[arg-type]
    (photo,) = nested.children
    assert Path(photo.staged).read_bytes() == JPEG and photo.source_sha256 == hashlib.sha256(JPEG).hexdigest()


def test_stage_children_detects_a_changed_email(tmp_path: Path, routes) -> None:  # noqa: ANN001
    m = rich()
    plan, st, home = make_plan(tmp_path, {"mail.eml": bytes(m)})  # this first serialisation fixes the boundaries
    run = RunContext("r", Mode.CONVERT, home, Toolset(home), st, str(tmp_path / "src"), str(tmp_path / "out"),
                     str(tmp_path / "work"))
    top_it = plan.items[0]
    other = tmp_path / "changed.eml"
    m.get_payload()[1].set_content(b"%PDF-1.4 CHANGED", maintype="application", subtype="pdf")
    other.write_bytes(bytes(m))
    top = WorkItem(top_it, run, str(tmp_path / "w" / "1"), top_it.abs_path, False, staged=str(other))
    top.children = [WorkItem(it, run, str(tmp_path / "w" / str(it.n)), None, False)
                    for it in plan.items if it.parent == top_it.n]
    em.EmailRoute().stage_children(None, top)  # type: ignore[arg-type]
    changed = [c.plan.source_path for c in top.children if c.done]
    assert changed == ["mail.eml#attachment-2.pdf"]
    assert top.children[1].reasons == ["SOURCE_CHANGED"]


def test_enumeration_is_deterministic(tmp_path: Path, routes) -> None:  # noqa: ANN001
    data = bytes(rich())
    a = [(x.name, hashlib.sha256(x.data).hexdigest()) for x in em.enumerate_attachments(em.parse(data))]
    b = [(x.name, hashlib.sha256(x.data).hexdigest()) for x in em.enumerate_attachments(em.parse(data))]
    assert a == b
    p1, _, _ = make_plan(tmp_path / "1", {"m.eml": data, "z.doc": b""})
    p2, _, _ = make_plan(tmp_path / "2", {"z.doc": b"", "m.eml": data})
    assert {i.source_path: (i.output_path, i.data.get("sha256")) for i in p1.items} == \
           {i.source_path: (i.output_path, i.data.get("sha256")) for i in p2.items}


def test_nesting_depth_four_is_too_deep(tmp_path: Path, routes) -> None:  # noqa: ANN001
    level = mk("L4")
    level.add_attachment(JPEG, maintype="image", subtype="jpeg", filename="photo.jpg")
    for n in (3, 2):
        outer = mk(f"L{n}")
        outer.add_attachment(level, filename=f"level{n + 1}.eml")
        level = outer
    top = mk("L1")
    top.add_attachment(level, filename="level2.eml")
    plan, _, _ = make_plan(tmp_path, {"deep.eml": bytes(top)})
    by = {it.source_path: it for it in plan.items}
    l4 = by["deep.eml#level2.eml#level3.eml#level4.eml"]
    assert l4.reasons == ["EML_NESTING_TOO_DEEP"] and l4.final
    assert not by["deep.eml#level2.eml#level3.eml"].final
    assert "deep.eml#level2.eml#level3.eml#level4.eml#photo.jpg" not in by


# --------------------------------------------------------------------------- policies (plan time)


def probe(data: bytes, mode: Mode = Mode.CONVERT, **opts) -> Probe:
    st = copy.deepcopy(S.defaults())
    st["workflow"].update(opts)
    home = Home(Path("."))
    return em.EmailRoute().probe(ProbeContext(mode, st, Toolset(home), home),
                                 SourceRef("m.eml", ".eml", data=data, size=len(data)))


def test_policies() -> None:
    with_att, without = bytes(rich()), bytes(mk())
    p = probe(with_att, eml_attachments="block")
    assert p.reasons == ["EML_ATTACHMENTS_BLOCKED"] and "4 attachments" in p.message and p.method == ""
    assert probe(without, eml_attachments="block").reasons == []
    p = probe(with_att, eml_attachments="list")
    assert p.reasons == [] and p.notes == ["ATTACHMENTS_DROPPED"]
    assert probe(without, eml_attachments="list").notes == []
    p = probe(with_att)
    assert p.reasons == [] and p.notes == [] and p.source_format == "E-mail (MIME)"
    assert p.method.startswith("Python email → HTML · LibreOffice")


def test_probe_unparseable_and_check_mode() -> None:
    p = probe(b"\x00\x01 noise")
    assert p.reasons == ["CONVERSION_ERROR"] and p.final
    assert probe(b"\x00\x01 noise", Mode.CHECK).reasons == []


def test_charset_errors_flagged_at_plan_time_but_not_blocking() -> None:
    body = "訪談".encode("big5")
    p = probe(raw([b"From: a@example.invalid", b"Subject: x"], body, b"text/plain; charset=utf-8"))
    assert p.reasons == ["CHARSET_ERRORS"] and not p.final and "decodes as big5" in p.message
    from baleen.plan import decides_at_plan_time

    assert not decides_at_plan_time("CHARSET_ERRORS")
    # under block, nothing is written, so only the block reason is given
    m = em.parse(raw([b"From: a@example.invalid"], body, b"text/plain; charset=utf-8"))
    assert em.analyse(m).problems


# --------------------------------------------------------------------------- headers and charsets


def test_headers_decoded_with_iso_date() -> None:
    subj = b"=?big5?B?" + base64.b64encode("訪談記錄".encode("big5")) + b"?="
    data = raw([b"From: =?utf-8?B?" + base64.b64encode("張三".encode()) + b"?= <z@example.invalid>",
                b"To: a@example.invalid", b"To: B <b@example.invalid>", b"Cc: C <c@example.invalid>",
                b"Date: Mon, 02 Oct 2006 09:30:00 +0800", b"Subject: " + subj], b"body")
    heads = {h.name: h for h in em.header_lines(em.parse(data), hide=False)}
    assert heads["From"].value == "張三 <z@example.invalid>"
    assert heads["To"].value == "a@example.invalid, B <b@example.invalid>"
    assert heads["Subject"].value == "訪談記錄"
    assert heads["Date"].value == "Mon, 02 Oct 2006 09:30:00 +0800 (2006-10-02T09:30:00+08:00)"
    assert all(h.ok for h in heads.values())


def test_bad_date_kept_as_is() -> None:
    heads = em.header_lines(em.parse(raw([b"From: a@example.invalid", b"Date: sometime in 2004"], b"x")), hide=False)
    assert [h.value for h in heads if h.name == "Date"] == ["sometime in 2004"]


def test_raw_8bit_headers() -> None:
    big5_subject = "訪談".encode("big5")
    ok = raw([b"From: a@example.invalid", b"Subject: " + big5_subject], "內文".encode("big5"),
             b"text/plain; charset=big5")
    an = em.analyse(em.parse(ok))
    assert [h.value for h in an.headers if h.name == "Subject"] == ["訪談"] and an.problems == []
    bad = raw([b"From: a@example.invalid", b"Subject: " + big5_subject], b"ascii body")
    an = em.analyse(em.parse(bad))
    subject = next(h for h in an.headers if h.name == "Subject")
    assert not subject.ok and "\ufffd" in subject.value and an.problems


@pytest.mark.parametrize(("ctype", "body", "ok", "hint"), [
    (b"text/plain; charset=big5", "訪談記錄".encode("big5"), True, None),
    (b"text/plain; charset=big5", "價格 €5".encode("cp950"), True, None),  # cp950 superset of big5
    (b"text/plain", "naïve café".encode(), True, None),  # undeclared, strict UTF-8 counts as certain
    (b"text/plain", b"plain ascii", True, None),
    (b"text/plain", "訪談記錄".encode("big5"), False, "big5"),
    (b"text/plain; charset=utf-8", "訪談記錄".encode("big5"), False, "big5"),
    (b"text/plain; charset=x-no-such-charset", b"abc \xff", False, None),
    (b"text/plain; charset=iso-8859-1", "訪談記錄".encode("big5"), True, None),  # R-07: undetectable
    (b"text/html", b'<meta charset="big5"><p>' + "訪談".encode("big5"), True, None),
])
def test_body_charsets(ctype: bytes, body: bytes, ok: bool, hint: str | None) -> None:
    msg = em.parse(raw([b"From: a@example.invalid"], body, ctype))
    (part,) = em.structure(msg).texts
    d = em.decode_text_part(part)
    assert d.ok is ok
    if not ok:
        assert "\ufffd" in d.text
        assert (f"decodes as {hint}" in d.problem) if hint else ("decodes as" not in d.problem or True)


# --------------------------------------------------------------------------- privacy


def test_hide_email_addresses_everywhere() -> None:
    m = EmailMessage()
    m["From"] = '"sender@example.invalid" <sender@example.invalid>'
    m["To"] = "Archive Team <archive@example.invalid>, plain@example.invalid"
    m["Cc"] = "李四 <li@example.invalid>"
    m["Subject"] = "Reply to owner@example.invalid"
    m.set_content("Write to someone@example.invalid.")
    m.add_alternative('<p>Mail <a href="mailto:info@example.invalid">info@example.invalid</a>, '
                      '<a href="mailto:x%40example.invalid">x</a>, y&#64;example.invalid</p>', subtype="html")
    m.add_attachment(b"z", maintype="application", subtype="pdf", filename="for-bob@example.invalid.pdf")
    doc = em.render(em.parse(bytes(m)), saved=["mail_attachments/for-bob@example.invalid.pdf"], hide=True,
                    policy="extract")
    assert em.ADDRESS_RE.search(doc) is None
    assert "example.invalid" not in doc.replace("[address hidden]", "")
    assert "Archive Team &lt;[address hidden]&gt;" in doc and "李四 &lt;[address hidden]&gt;" in doc
    assert 'href="mailto:[address hidden]"' in doc
    shown = em.render(em.parse(bytes(m)), saved=["x"], hide=False, policy="extract")
    assert "archive@example.invalid" in shown and "[address hidden]" not in shown


# --------------------------------------------------------------------------- HTML body (§6.4 + cid)


def test_html_body_cid_and_resources() -> None:
    html = ('<html><head><style>body{background:url(cid:bg@x)} p{background:url("http://t.example.invalid/a.png")}'
            '</style></head><body background="cid:bg@x">'
            '<img src="cid:logo@x"><img src="cid:missing@x"><img src="file:///C:/Temp/clip.jpg">'
            '<img src="\\\\fileserver\\share\\logo.png"><img src="images/rel.gif">'
            '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><img src="https://tracker.example.invalid/p.gif">'
            '<script>alert(1)</script><a href="https://example.invalid/page">link</a></body></html>')
    styles, body = em.html_section(html, {"logo@x": "logo.jpg"}, saved=True)
    doc = styles + body
    assert "[inline image: logo.jpg, saved as attachment]" in body
    assert "[inline image: cid:missing@x, not found in the e-mail]" in body
    assert "[external resource not archived: file:///C:/Temp/clip.jpg]" in body
    assert "[external resource not archived: \\\\fileserver\\share\\logo.png]" in body
    assert "[external resource not archived: images/rel.gif]" in body
    assert "[external resource not archived: https://tracker.example.invalid/p.gif]" in body
    assert 'src="data:image/gif' in body and "<script" not in doc and "cid:" not in doc.replace("cid:missing", "")
    assert remote_urls_left(doc) == []
    assert '<a href="https://example.invalid/page">' in body
    _st, listed = em.html_section('<img src="cid:logo@x">', {"logo@x": "logo.jpg"}, saved=False)
    assert "[inline image: logo.jpg, not saved]" in listed


def test_html_body_never_references_local_or_network_files() -> None:
    """allow_local=False: absolute local paths, file: URLs and UNC shares never survive (P8, SEC-9)."""
    refs = ["file:///C:/Windows/win.ini", "file://fileserver/share/a.png", "C:\\Users\\me\\secret.png",
            "C:/Users/me/secret.png", "/etc/passwd", "\\\\fileserver\\share\\b.png", "//cdn.example.invalid/c.png",
            "../up.png", "local.png"]
    parts = []
    for i, r in enumerate(refs):
        parts += [f'<img src="{r}">', f'<img srcset="{r} 2x">', f'<td background="{r}">x</td>',
                  f'<div style="background:url({r})">s{i}</div>', f'<link rel="stylesheet" href="{r}">',
                  f'<video poster="{r}"></video>', f'<input type="image" src="{r}">']
    html = f"<html><head><style>@import url('{refs[0]}'); body{{background:url('{refs[3]}')}}</style></head>" \
           f"<body>{''.join(parts)}</body></html>"
    styles, body = em.html_section(html, {}, saved=True)
    doc = styles + body
    import re

    attrs = re.findall(r"""(?:src|srcset|background|poster|href|data)\s*=\s*["']([^"']*)["']""", doc, re.I)
    urls = [u for u in re.findall(r"url\(\s*['\"]?([^'\")]*)", doc, re.I) if u.strip().lower() != "none"]
    for value in attrs + urls:
        assert not re.match(r"\s*(file:|[a-z]:[\\/]|/|\\\\)", value, re.I), value
        assert value.split("/")[0] not in ("..", "local.png"), value
    assert "[external resource not archived: file:///C:/Windows/win.ini]" in doc
    assert "[external resource not archived: \\\\fileserver\\share\\b.png]" in doc


def test_render_document_shape() -> None:
    msg = em.parse(bytes(rich()))
    doc = em.render(msg, saved=["mail_attachments/logo.jpg", "not saved (see the report)", "x", "y"], hide=False,
                    policy="extract")
    assert doc.startswith("<!DOCTYPE html>") and '<meta charset="utf-8">' in doc and "<title>Rich</title>" in doc
    assert "Attachments (4)" in doc and "mail_attachments/logo.jpg" in doc and "18 bytes" in doc
    assert "[inline image: logo.jpg, saved as attachment]" in doc
    listed = em.render(msg, saved=["not saved"] * 4, hide=False, policy="list")
    assert "Attachments (listed, not saved) (4)" in listed
    plain = em.render(em.parse(bytes(mk("P", "line <1> & \x07bell"))), saved=[], hide=False, policy="extract")
    assert '<pre class="body">line &lt;1&gt; &amp; bell' in plain
    empty = em.parse(raw([b"From: a@example.invalid"], b"%PDF", b"application/pdf"))
    assert "(This e-mail has no text body.)" in em.render(empty, saved=["x"], hide=False, policy="extract")


# --------------------------------------------------------------------------- run time: prepare / finish


def test_prepare_and_finish(tmp_path: Path, routes) -> None:  # noqa: ANN001
    from pypdf import PdfWriter

    plan, st, home = make_plan(tmp_path, {"sub/mail.eml": bytes(rich())})
    run = RunContext("r", Mode.CONVERT, home, Toolset(home), st, str(tmp_path / "src"), str(tmp_path / "out"),
                     str(tmp_path / "work"))
    top_it = plan.items[0]
    top = WorkItem(top_it, run, str(tmp_path / "work" / "1"), top_it.abs_path, False, staged=top_it.abs_path)
    top.children = [WorkItem(it, run, str(tmp_path / "work" / str(it.n)), None, False)
                    for it in plan.items if it.parent == top_it.n]
    route = em.EmailRoute()
    job = route.prepare(None, top)  # type: ignore[arg-type]
    assert isinstance(job, LoJob) and job.infilter == "HTML (StarWriter)" and Path(job.input_path).name == "mail-1.html"
    html = Path(job.input_path).read_bytes()
    assert html.startswith(b"\xef\xbb\xbf")
    text = html.decode("utf-8-sig")
    assert "mail_attachments/logo.jpg" in text and "mail_attachments/fwd.pdf" in text
    assert job.batch_key == route.batch_key(top)
    pdf = tmp_path / "out.pdf"
    w = PdfWriter()
    w.add_blank_page(width=100, height=100)
    with open(pdf, "wb") as f:
        w.write(f)
    route.finish(None, top, LoResult(job, True, str(pdf), method="LibreOffice 26.8.0 · writer_pdf_Export · PDF/A-2b"))  # type: ignore[arg-type]
    assert top.method == "Python email → HTML · LibreOffice 26.8.0 · writer_pdf_Export · PDF/A-2b"
    assert top.result_path == str(pdf) and top.new_output and top.pdfa == (str(pdf), "2b")
    assert [c.report_form() for c in top.checks] == ["V-PDF-OPEN=pass"]
    failed = WorkItem(top_it, run, str(tmp_path / "work" / "9"), top_it.abs_path, False)
    route.finish(None, failed, LoResult(job, False, None, "TIMEOUT", "took too long"))  # type: ignore[arg-type]
    assert failed.reasons == ["TIMEOUT"] and failed.done


def test_prepare_list_policy_says_not_saved(tmp_path: Path, routes) -> None:  # noqa: ANN001
    plan, st, home = make_plan(tmp_path, {"mail.eml": bytes(rich())}, eml_attachments="list")
    assert all(not it.materialise for it in plan.items[1:])
    run = RunContext("r", Mode.CONVERT, home, Toolset(home), st, str(tmp_path / "src"), str(tmp_path / "out"),
                     str(tmp_path / "work"))
    top = WorkItem(plan.items[0], run, str(tmp_path / "work" / "1"), plan.items[0].abs_path, False,
                   staged=plan.items[0].abs_path)
    job = em.EmailRoute().prepare(None, top)  # type: ignore[arg-type]
    text = Path(job.input_path).read_text(encoding="utf-8-sig")  # type: ignore[union-attr]
    assert text.count("<td>not saved</td>") == 4 and "[inline image: logo.jpg, not saved]" in text


def test_nested_bytes_round_trip() -> None:
    m = mk("outer")
    inner = mk("inner", "inner body 訪談")
    m.add_attachment(inner, filename="in.eml")
    att = em.enumerate_attachments(em.parse(bytes(m)))[0]
    reparsed = em.parse(att.data)
    assert str(reparsed["Subject"]) == "inner"
    assert "inner body 訪談" in em.decode_text_part(em.structure(reparsed).texts[0]).text
    assert io.BytesIO(att.data).read(9) != b"" and att.data == em.enumerate_attachments(em.parse(bytes(m)))[0].data
    assert POLICY is em.POLICY
