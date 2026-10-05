"""E-mail route (spec §6.5, DR-10): .eml -> one HTML document -> PDF/A with LibreOffice,
plus each attachment as its own item.

Documents lane, batched with the HTML import filter (§6.2). The steps:

- Parse with Python `email` (policy.default). A file without any header -> FAILED CONVERSION_ERROR.
- Render one HTML document: the headers From, To, Cc, Date (original string plus ISO 8601 when
  it parses) and Subject, all RFC 2047-decoded; an attachments block (name, size, saved relative
  path or "not saved"); and the body: the text/html alternative sanitised as in §6.4, otherwise
  text/plain, preformatted (Writer wraps long lines). `cid:` images become
  "[inline image: name, saved as attachment]" and are attachments themselves.
- Charsets: each body part is decoded strictly with its declared charset (WHATWG-style supersets
  such as big5 -> cp950/big5-hkscs count as the declared charset). Undeclared text is us-ascii
  (RFC 2045), and, as in §6.3, a strict UTF-8 decode also counts as certain. Raw 8-bit header
  bytes are tried as UTF-8, then as the message's declared body charset. Anything else is
  decoded with replacement characters and flagged NEEDS_REVIEW CHARSET_ERRORS; the PDF is still
  written. The check runs at plan time (Preview shows it, a resumed run keeps it).
- hide_email_addresses: every address in the PDF (headers, body, links, attachment names)
  becomes "[address hidden]"; display names are kept.
- eml_attachments: extract (children via expand(), written by stage_children(), output into
  <final e-mail stem>_attachments/, nested message/rfc822 recursively), block (decided at plan
  time when there are attachments, nothing written), list (OK + note ATTACHMENTS_DROPPED).

Attachments (§6.5): any part with Content-Disposition: attachment, any non-text part (with or
without a filename; cid: inline images are non-text parts), and every message/rfc822 part (a
nested e-mail, always named *.eml). expand() and stage_children() share enumerate_attachments(),
which is deterministic (P7): plan-time names and run-time files always match.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import io
import posixpath
import quopri
import re
from dataclasses import dataclass, field
from email import policy as email_policy
from email.generator import BytesGenerator
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import unquote

from ..model import Action, Category, Mode, Probe
from ..paths import long_path
from ..scheduler import Lane, TaskContext
from .base import ChildSpec, LoJob, LoResult, ProbeContext, Route, SourceRef, WorkItem, register
from .document import check_existing_pdf, pdf_finish
from .names import sanitise_attachment_name
from .sanitize import is_remote, placeholder, sanitize_html

POLICY = email_policy.default
NESTED_TYPES = ("message/rfc822", "message/global")
HIDDEN = "[address hidden]"
MAX_EMAIL_BYTES = 1 << 30

# Extension for an attachment without a filename, from its MIME type (fixed table, P7: the
# platform's mimetypes database differs between machines).
MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg", "image/pjpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/x-bmp": ".bmp", "image/x-ms-bmp": ".bmp", "image/tiff": ".tif",
    "image/webp": ".webp", "image/heic": ".heic", "image/svg+xml": ".svg",
    "application/pdf": ".pdf", "application/msword": ".doc", "application/rtf": ".rtf", "text/rtf": ".rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt", "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp", "application/wordperfect": ".wpd",
    "application/vnd.ms-works": ".wps",
    "text/plain": ".txt", "text/html": ".html", "text/csv": ".csv", "text/calendar": ".ics", "text/vcard": ".vcf",
    "text/x-vcard": ".vcf", "message/rfc822": ".eml", "message/global": ".eml",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/x-ms-wma": ".wma",
    "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/amr": ".amr", "audio/basic": ".au",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/avi": ".avi",
    "video/mpeg": ".mpg", "video/x-ms-wmv": ".wmv", "video/x-ms-asf": ".asf", "video/3gpp": ".3gp",
    "video/webm": ".webm", "application/zip": ".zip", "application/x-zip-compressed": ".zip",
    "application/octet-stream": ".bin", "application/ms-tnef": ".dat", "application/vnd.ms-tnef": ".dat",
    "application/pgp-signature": ".asc", "application/pkcs7-signature": ".p7s",
    "application/x-pkcs7-signature": ".p7s",
}

# Declared charset -> codecs tried strictly, in order (WHATWG Encoding Standard practice:
# common labels decode as their usual superset).
SUPERSETS: dict[str, tuple[str, ...]] = {
    "big5": ("big5", "cp950", "big5hkscs"), "x-x-big5": ("big5", "cp950", "big5hkscs"),
    "cn-big5": ("big5", "cp950", "big5hkscs"), "big5-hkscs": ("big5hkscs",),
    "gb2312": ("gb2312", "gbk", "gb18030"), "gbk": ("gbk", "gb18030"), "x-gbk": ("gbk", "gb18030"),
    "euc-cn": ("gb2312", "gbk", "gb18030"), "gb18030": ("gb18030",),
    "shift_jis": ("shift_jis", "cp932"), "shift-jis": ("shift_jis", "cp932"), "sjis": ("shift_jis", "cp932"),
    "x-sjis": ("shift_jis", "cp932"), "ms_kanji": ("cp932",), "windows-31j": ("cp932",),
    "euc-kr": ("euc_kr", "cp949"), "ks_c_5601-1987": ("cp949",), "iso-2022-jp": ("iso2022_jp", "iso2022_jp_ext"),
    "iso-8859-1": ("cp1252", "latin-1"), "latin1": ("cp1252", "latin-1"), "iso_8859-1": ("cp1252", "latin-1"),
    "us-ascii": ("ascii", "utf-8"), "ascii": ("ascii", "utf-8"), "ansi_x3.4-1968": ("ascii", "utf-8"),
    "tis-620": ("cp874",), "windows-874": ("cp874",),
}
HINT_CODECS = ("big5", "gb18030", "shift_jis", "windows-1252")  # as §6.3

ADDRESS_RE = re.compile(
    r"[A-Za-z0-9._%+'-]+(?:@|%40|&#64;|&#x40;|&commat;)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", re.I)


class Unparseable(Exception):
    """The file is not an e-mail Python's parser can read (§6.5 -> CONVERSION_ERROR)."""


# --------------------------------------------------------------------------- parsing


def parse(data: bytes) -> EmailMessage:
    try:
        msg = BytesParser(policy=POLICY).parsebytes(data)
    except Exception as e:  # the parser is lenient; anything raised means unreadable
        raise Unparseable(f"{e.__class__.__name__}: {e}") from e
    if not isinstance(msg, Message) or len(msg.keys()) == 0:
        raise Unparseable("No e-mail headers found.")
    return msg  # type: ignore[return-value]


def _has_surrogates(s: str) -> bool:
    return any(0xDC80 <= ord(c) <= 0xDCFF for c in s)


def _decode_raw_header(value: str, fallbacks: list[str]) -> tuple[str, bool]:
    """Raw 8-bit header bytes (surrogate-escaped by the parser): UTF-8, then fallbacks, strictly."""
    if not _has_surrogates(value):
        return value, True
    raw = value.encode("ascii", "surrogateescape")
    for cs in ("utf-8", *fallbacks):
        try:
            return raw.decode(cs), True
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace"), False


def _header_objects(part: Message, name: str, fallbacks: list[str]) -> tuple[list, bool]:
    """Header objects for every `name` header, with raw 8-bit bytes decoded; ok=False on errors."""
    objs, ok = [], True
    for k, v in part.raw_items():
        if k.lower() != name.lower():
            continue
        if hasattr(v, "name"):  # already a header object
            text, good = str(v), True
        else:
            text, good = _decode_raw_header(re.sub(r"[\r\n]", "", v), fallbacks)
        ok &= good
        try:
            h = POLICY.header_factory(name, text)
        except Exception:
            objs.append(text)
            continue
        if any(type(d).__name__ == "UndecodableBytesDefect" for d in getattr(h, "defects", ())):
            ok = False
        if "�" in str(h) and "�" not in text:
            ok = False
        objs.append(h)
    return objs, ok


def _content_id(part: Message) -> str | None:
    v = part.get("Content-ID")
    if not v:
        return None
    s = str(v).strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    return s.strip() or None


# --------------------------------------------------------------------------- structure


@dataclass
class Attachment:
    index: int  # 1-based order among the e-mail's attachments (§6.5 attachment-<n>)
    name: str  # sanitised (§7.8)
    data: bytes
    mime: str
    cid: str | None = None
    nested: bool = False
    name_ok: bool = True  # False when the filename had undecodable bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Structure:
    texts: list[Message] = field(default_factory=list)  # body parts to render, in order
    attachments: list[Message] = field(default_factory=list)


def _contains_html(part: Message) -> bool:
    if part.get_content_type() == "text/html":
        return True
    if part.is_multipart() and part.get_content_type() not in NESTED_TYPES:
        return any(_contains_html(p) for p in part.iter_parts())
    return False


def _choose_alternative(subs: list[Message]) -> Message | None:
    """RFC 2046: the last alternative is the richest. Prefer HTML, then plain text."""
    for pred in (_contains_html, lambda p: p.get_content_maintype() == "text" or p.is_multipart()):
        for p in reversed(subs):
            if p.get_content_type() not in NESTED_TYPES and pred(p):
                return p
    return subs[-1] if subs else None


def _is_attachment_leaf(part: Message) -> bool:
    if part.get_content_disposition() == "attachment":
        return True
    return part.get_content_maintype() != "text"


def _walk(part: Message, render: bool, out: Structure, depth: int = 0) -> None:
    if depth > 100:
        return
    ctype = part.get_content_type()
    if ctype in NESTED_TYPES:
        out.attachments.append(part)
        return
    if part.get_content_maintype() == "message":
        # message/delivery-status, message/disposition-notification, ...: header blocks as text
        if render:
            out.texts.append(part)
        return
    if part.is_multipart():
        subs = list(part.iter_parts())
        if ctype == "multipart/alternative":
            chosen = _choose_alternative(subs)
            for s in subs:
                _walk(s, render and s is chosen, out, depth + 1)
        elif ctype == "multipart/related" and subs:
            start = part.get_param("start")
            root = subs[0]
            if start:
                for s in subs:
                    if str(s.get("Content-ID", "")).strip() == str(start).strip():
                        root = s
            for s in subs:
                # resources of the root (images...) are attachments; extra text parts are not rendered
                _walk(s, render and s is root, out, depth + 1)
        else:
            for s in subs:
                _walk(s, render, out, depth + 1)
        return
    if _is_attachment_leaf(part):
        out.attachments.append(part)
        return
    if render:
        out.texts.append(part)


def structure(msg: Message) -> Structure:
    out = Structure()
    _walk(msg, True, out)
    return out


def _nested_bytes(part: Message) -> bytes:
    """The bytes of a nested message/rfc822 part (deterministic re-serialisation)."""
    payload = part.get_payload()
    inner = payload[0] if isinstance(payload, list) and payload else None
    cte = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if inner is None:
        return b""
    if cte in ("base64", "quoted-printable") and len(inner.keys()) == 0:
        # Non-standard but seen: an encoded message/rfc822 body. Decode it ourselves.
        body = inner.get_payload()
        raw = body.encode("ascii", "surrogateescape") if isinstance(body, str) else b""
        try:
            return base64.b64decode(raw) if cte == "base64" else quopri.decodestring(raw)
        except (binascii.Error, ValueError):
            pass
    buf = io.BytesIO()
    BytesGenerator(buf, mangle_from_=False, policy=inner.policy.clone(linesep="\r\n", refold_source="none")
                   ).flatten(inner)
    return buf.getvalue()


def message_charset(msg: Message) -> str | None:
    """The first declared non-ASCII charset of a text part: the fallback for raw 8-bit headers."""
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            cs = part.get_content_charset()
            if cs and cs not in ("us-ascii", "ascii", "utf-8"):
                return cs
    return None


def _filename(part: Message, fallbacks: list[str]) -> tuple[str | None, bool]:
    for header, param in (("Content-Disposition", "filename"), ("Content-Type", "name")):
        objs, ok = _header_objects(part, header, fallbacks)
        for h in objs:
            params = getattr(h, "params", None) or {}
            v = params.get(param)
            if v:
                return str(v), ok
    try:
        v = part.get_filename()
    except Exception:
        v = None
    return (str(v) if v else None), True


def enumerate_attachments(msg: Message) -> list[Attachment]:
    """Every attachment of `msg`, in MIME order, named per §6.5/§7.8. Deterministic (P7)."""
    fallbacks = [cs for cs in (message_charset(msg),) if cs]
    out: list[Attachment] = []
    for i, part in enumerate(structure(msg).attachments, start=1):
        mime = part.get_content_type()
        nested = mime in NESTED_TYPES
        if nested:
            data = _nested_bytes(part)
        else:
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:
                data = b""
            if not isinstance(data, bytes):
                data = bytes(data)
        raw_name, name_ok = _filename(part, fallbacks + [cs for cs in (part.get_content_charset(),) if cs])
        ext = MIME_EXT.get(mime, "")
        name = sanitise_attachment_name(raw_name, i, ext)
        if nested and not name.lower().endswith(".eml"):
            name = sanitise_attachment_name(name + ".eml", i, ".eml")
        out.append(Attachment(i, name, data, mime, _content_id(part), nested, name_ok))
    return out


# --------------------------------------------------------------------------- charsets


def _codecs_for(declared: str | None) -> list[str]:
    if not declared:
        return ["ascii", "utf-8"]
    d = declared.strip().lower()
    return list(SUPERSETS.get(d, (d,)))


def _hint(raw: bytes) -> str | None:
    for cs in HINT_CODECS:
        try:
            raw.decode(cs)
            return cs
        except UnicodeDecodeError:
            continue
    return None


def _meta_charset(raw: bytes) -> str | None:
    m = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", raw[:4096], re.I)
    return m.group(1).decode("ascii", "replace").lower() if m else None


@dataclass
class Decoded:
    text: str
    ok: bool
    problem: str = ""


def decode_text_part(part: Message) -> Decoded:
    """§6.5 Charset: strict decode with the declared charset; on failure replacement characters."""
    if part.get_content_maintype() == "message":
        payload = part.get_payload()
        if isinstance(payload, list):
            return Decoded("\n".join(str(p) for p in payload), True)
    try:
        raw = part.get_payload(decode=True) or b""
    except Exception:
        raw = b""
    if not isinstance(raw, bytes):
        raw = bytes(raw)
    declared = part.get_content_charset()
    if declared is None and part.get_content_type() == "text/html":
        declared = _meta_charset(raw)
    tried = _codecs_for(declared)
    for cs in tried:
        try:
            return Decoded(raw.decode(cs), True)
        except UnicodeDecodeError:
            continue
        except LookupError:
            break
    try:
        text = raw.decode(tried[0], "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    label = f"declared {declared}" if declared else "no charset declared"
    hint = _hint(raw)
    problem = f"Part of the body isn't valid text in its charset ({label}); undecodable bytes show as �."
    if hint and hint != (declared or "").lower():
        problem += f" It decodes as {hint}."
    return Decoded(text, False, problem)


# --------------------------------------------------------------------------- rendering

HEADER_NAMES = ("From", "To", "Cc", "Date", "Subject")
ADDRESS_HEADERS = ("From", "To", "Cc")


def hide_addresses(text: str) -> str:
    return ADDRESS_RE.sub(HIDDEN, text)


def _fmt_address(a) -> str:  # noqa: ANN001
    name = (a.display_name or "").strip()
    return f"{name} <{HIDDEN}>" if name else HIDDEN


@dataclass
class HeaderLine:
    name: str
    value: str
    ok: bool = True


def header_lines(msg: Message, *, hide: bool) -> list[HeaderLine]:
    fallbacks = [cs for cs in (message_charset(msg),) if cs]
    out: list[HeaderLine] = []
    for name in HEADER_NAMES:
        objs, ok = _header_objects(msg, name, fallbacks)
        if not objs:
            continue
        parts: list[str] = []
        for h in objs:
            if name in ADDRESS_HEADERS and hide and getattr(h, "addresses", None):
                parts.append(", ".join(_fmt_address(a) for a in h.addresses))
            else:
                parts.append(str(h))
        value = ", ".join(p for p in parts if p)
        if name == "Date":
            try:
                dt = parsedate_to_datetime(value)
                value = f"{value} ({dt.isoformat()})"
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
        if hide:
            value = hide_addresses(value)
        out.append(HeaderLine(name, value, ok))
    return out


class _EmailHtml(HTMLParser):
    """Pre-pass over an e-mail's HTML before the §6.4 sanitiser.

    - <img src="cid:..."> -> "[inline image: name, saved as attachment]" (or "not saved").
    - Resource URLs that are neither data: nor remote (file:, \\\\server\\share, relative paths,
      other schemes) have no meaning outside the sender's machine and could reach a network
      share: images become the §6.4 placeholder text, other such attributes are dropped.
    Remote URLs are left for sanitize_html(), which replaces them with the placeholder.
    """

    RES_ATTRS = ("src", "background", "poster", "lowsrc", "dynsrc", "data")

    def __init__(self, cid_names: dict[str, str], saved: bool) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.cid_names = cid_names
        self.saved = saved
        self.in_style = False

    @staticmethod
    def _local(v: str) -> bool:
        s = v.strip().lower()
        return bool(s) and not s.startswith("data:") and not is_remote(v) and not s.startswith("#")

    def _cid_text(self, url: str) -> str:
        key = unquote(url.strip()[4:]).strip().strip("<>")
        name = self.cid_names.get(key) or self.cid_names.get(key.lower())
        if name is None:
            return f"[inline image: cid:{key}, not found in the e-mail]"
        return f"[inline image: {name}, {'saved as attachment' if self.saved else 'not saved'}]"

    def _css(self, css: str) -> str:
        def repl(m: re.Match[str]) -> str:
            url = m.group(2)
            return "none" if url.strip().lower().startswith("cid:") or self._local(url) else m.group(0)

        return re.sub(r"url\(\s*(['\"]?)(.*?)\1\s*\)", repl, css, flags=re.I | re.S)

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]], close: bool) -> None:
        src = next((v for k, v in attrs if k.lower() == "src" and v), None)
        is_image = tag in ("img", "image") or (tag == "input" and any(
            k.lower() == "type" and (v or "").lower() == "image" for k, v in attrs))
        if is_image and src is not None:
            if src.strip().lower().startswith("cid:"):
                self.out.append(f"<span>{html.escape(self._cid_text(src))}</span>")
                return
            if self._local(src):
                self.out.append(f"<span>{html.escape(placeholder(src))}</span>")
                return
        if tag == "link" and any(k.lower() == "href" and v and self._local(v) for k, v in attrs):
            return
        changed = False
        kept: list[str] = []
        for k, v in attrs:
            kl = k.lower()
            if v is not None and kl in self.RES_ATTRS and (v.strip().lower().startswith("cid:") or self._local(v)):
                changed = True
                continue
            if v is not None and kl == "srcset":
                cands = [c.strip() for c in v.split(",") if c.strip()]
                keep = [c for c in cands if not (c.split()[0].lower().startswith("cid:") or self._local(c.split()[0]))]
                if keep != cands:
                    changed = True
                    if not keep:
                        continue
                    v = ", ".join(keep)
            if v is not None and kl == "style":
                nv = self._css(v)
                changed |= nv != v
                v = nv
            kept.append(k if v is None else f'{k}="{html.escape(v, quote=True)}"')
        if changed:
            self.out.append(f"<{tag}{' ' if kept else ''}{' '.join(kept)}{' /' if close else ''}>")
        else:
            self.out.append(self.get_starttag_text() or f"<{tag}>")
        if tag == "style":
            self.in_style = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs, False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs, True)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.in_style = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(self._css(data) if self.in_style else data)

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")

    def handle_comment(self, data: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return


def _strip_controls(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    return "".join(c for c in text if c in "\t\n" or not (ord(c) < 32 or ord(c) == 127))


def html_section(markup: str, cid_names: dict[str, str], saved: bool) -> tuple[str, str]:
    """(styles, body) of one sanitised HTML body part."""
    pre = _EmailHtml(cid_names, saved)
    pre.feed(_strip_controls(markup))
    pre.close()
    # allow_local=False and no base_href: a message can never pull files from this computer
    # (file:, C:\, /, \\host) into its PDF (P8, SEC-9).
    doc = sanitize_html("".join(pre.out), allow_local=False)
    m = re.search(r"<body[^>]*>", doc, re.I)
    head = doc[:m.start()] if m else ""
    styles = "".join(re.findall(r"<style[^>]*>.*?</style\s*>", head, re.I | re.S))
    if m:
        end = doc.lower().rfind("</body")
        body = doc[m.end(): end if end > m.end() else len(doc)]
    else:
        body = doc
    body = re.sub(r"</html\s*>\s*$", "", body, flags=re.I)
    return styles, body


def text_section(text: str) -> str:
    return f'<pre class="body">{html.escape(_strip_controls(text))}</pre>'


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n:,} bytes"
    for unit, div in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < div * 1024 or unit == "GB":
            return f"{n / div:.1f} {unit} ({n:,} bytes)"
    return f"{n:,} bytes"


CSS = """
body { font-family: 'Liberation Sans', Arial, sans-serif; font-size: 10.5pt; }
table.hdr { border-collapse: collapse; margin-bottom: 8pt; }
table.hdr th { text-align: left; vertical-align: top; padding: 1pt 8pt 1pt 0; }
table.hdr td { vertical-align: top; padding: 1pt 0; }
table.att { border-collapse: collapse; margin-bottom: 8pt; }
table.att th, table.att td { text-align: left; vertical-align: top; padding: 1pt 10pt 1pt 0; font-size: 9.5pt; }
pre.body { white-space: pre-wrap; font-size: 9.5pt; }
"""


@dataclass
class Analysis:
    """What plan time and run time both need: decoded headers, body parts, attachments, problems."""

    headers: list[HeaderLine]
    sections: list[tuple[Message, Decoded]]
    attachments: list[Attachment]
    problems: list[str]


def analyse(msg: Message, *, hide: bool = False) -> Analysis:
    heads = header_lines(msg, hide=hide)
    st = structure(msg)
    sections = [(p, decode_text_part(p)) for p in st.texts]
    atts = enumerate_attachments(msg)
    problems: list[str] = []
    if any(not h.ok for h in heads):
        problems.append("A header has undecodable bytes; they show as �.")
    for _p, d in sections:
        if not d.ok and d.problem not in problems:
            problems.append(d.problem)
    if any(not a.name_ok for a in atts):
        problems.append("An attachment name has undecodable bytes; they show as �.")
    return Analysis(heads, sections, atts, problems)


def render(msg: Message, *, saved: list[str], hide: bool, policy: str, analysis: Analysis | None = None) -> str:
    """The HTML document for one e-mail (§6.5). `saved[i]` describes attachment i+1's output."""
    a = analysis or analyse(msg, hide=hide)
    title = next((h.value for h in a.headers if h.name == "Subject"), "") or "E-mail"
    rows = "".join(f"<tr><th>{html.escape(h.name)}:</th><td>{html.escape(h.value)}</td></tr>" for h in a.headers)
    parts = [f'<table class="hdr">{rows}</table>']
    if a.attachments:
        label = "Attachments" if policy == "extract" else "Attachments (listed, not saved)"
        arows = "".join(
            f"<tr><td>{html.escape(att.name)}</td><td>{html.escape(human_size(att.size))}</td>"
            f"<td>{html.escape(saved[i] if i < len(saved) else 'not saved')}</td></tr>"
            for i, att in enumerate(a.attachments))
        parts.append(f"<p><b>{label} ({len(a.attachments)})</b></p>"
                     f'<table class="att"><tr><th>Name</th><th>Size</th><th>Saved as</th></tr>{arows}</table>')
    parts.append("<hr>")
    cid_names = {}
    for att in a.attachments:
        if att.cid:
            cid_names.setdefault(att.cid, att.name)
            cid_names.setdefault(att.cid.lower(), att.name)
    styles: list[str] = []
    body: list[str] = []
    for part, dec in a.sections:
        if part.get_content_type() == "text/html":
            st, b = html_section(dec.text, cid_names, policy == "extract")
            styles.append(st)
            body.append(f"<div>{b}</div>")
        else:
            body.append(text_section(dec.text))
    if not body:
        body.append("<p><i>(This e-mail has no text body.)</i></p>")
    parts.append("<hr>".join(body))
    doc = ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
           f"<title>{html.escape(title)}</title><style>{CSS}</style>{''.join(styles)}</head>"
           f"<body>{''.join(parts)}</body></html>\n")
    return hide_addresses(doc) if hide else doc


# --------------------------------------------------------------------------- the route


def _read(path: str) -> bytes:
    with open(long_path(path), "rb") as f:
        return f.read(MAX_EMAIL_BYTES)


def _read_ref(src: SourceRef) -> bytes:
    if src.data is not None:
        return src.data
    with src.open() as f:
        return f.read(MAX_EMAIL_BYTES)


def source_format(msg: Message) -> str:
    return "E-mail (MIME)" if msg.get("MIME-Version") else "E-mail (RFC 822)"


class EmailRoute(Route):
    key: ClassVar[str] = "email"
    lane: ClassVar[Lane] = Lane.DOCUMENTS
    converter_tools: ClassVar[tuple[str, ...]] = ("libreoffice",)
    batched: ClassVar[bool] = True
    infilter: ClassVar[str | None] = "HTML (StarWriter)"

    METHOD_PREFIX: ClassVar[str] = "Python email → HTML · "

    def probe(self, ctx: ProbeContext, src: SourceRef) -> Probe:
        level = ctx.workflow.get("pdfa_level", "2b")
        pr = Probe(Category.EMAIL, ".pdf", Action.CONVERT, "E-mail",
                   method=f"{self.METHOD_PREFIX}LibreOffice · writer_pdf_Export · PDF/A-{level}", route=self.key)
        if ctx.mode != Mode.CONVERT:
            return pr
        try:
            msg = parse(_read_ref(src))
        except OSError as e:
            pr.reasons, pr.final, pr.method = ["SOURCE_UNREADABLE"], True, ""
            pr.message = f"Can't read the source: {e.strerror or e}"
            return pr
        except Unparseable as e:
            pr.reasons, pr.final, pr.method = ["CONVERSION_ERROR"], True, ""
            pr.message = f"This isn't an e-mail Baleen can read: {e}"
            return pr
        pr.source_format = source_format(msg)
        try:
            an = analyse(msg)
        except Exception as e:
            pr.reasons, pr.final, pr.method = ["CONVERSION_ERROR"], True, ""
            pr.message = f"Couldn't read the e-mail's structure: {e.__class__.__name__}: {e}"
            return pr
        n = len(an.attachments)
        pol = ctx.workflow.get("eml_attachments", "extract")
        pr.data = {"attachments": n}
        if n and pol == "block":
            pr.reasons, pr.method = ["EML_ATTACHMENTS_BLOCKED"], ""
            pr.message = (f"This e-mail has {n} attachment{'s' if n != 1 else ''}; "
                          "E-mail attachments is set to Block, so nothing was written.")
            return pr
        if n and pol == "list":
            pr.notes = ["ATTACHMENTS_DROPPED"]
        if an.problems:
            pr.reasons = ["CHARSET_ERRORS"]
            pr.message = " ".join(an.problems)
        return pr

    def expand(self, ctx: ProbeContext, item, src: SourceRef) -> list[ChildSpec]:  # noqa: ANN001
        msg = parse(_read_ref(src))
        return [ChildSpec(name=a.name, data=a.data, depth=item.depth + 1,
                          data_hint={"index": a.index, "mime": a.mime, "cid": a.cid, "nested": a.nested})
                for a in enumerate_attachments(msg)]

    def stage_children(self, ctx: TaskContext, work: WorkItem) -> None:
        """Write each attachment into its child's work folder (run time, same enumeration as expand())."""
        path = work.staged or work.source_abs
        if not path:
            raise ValueError("the e-mail has no staged copy")
        atts = enumerate_attachments(parse(_read(path)))
        by_index = {a.index: a for a in atts}
        for pos, child in enumerate(work.children):
            idx = (child.plan.data.get("child") or {}).get("index") or pos + 1
            a = by_index.get(idx)
            name = child.plan.source_path.rsplit("#", 1)[-1]
            digest = hashlib.sha256(a.data).hexdigest() if a is not None else ""
            if a is None or a.name != name or digest != child.plan.data.get("sha256"):
                child.fail("SOURCE_CHANGED", "The attachment differs from the one found when the run was planned.")
                continue
            dst = child.out("input" + (child.plan.ext or ""))
            with open(long_path(dst), "wb") as f:
                f.write(a.data)
            child.staged = dst
            child.source_sha256 = digest
            child.source_size = len(a.data)

    def _saved(self, work: WorkItem, atts: list[Attachment], pol: str) -> list[str]:
        if pol != "extract":
            return ["not saved"] * len(atts)
        kids = {(c.plan.data.get("child") or {}).get("index"): c for c in work.children}
        base = posixpath.dirname(work.plan.output_path or "")
        out = []
        for a in atts:
            c = kids.get(a.index)
            if c is not None and c.plan.output_path and not c.plan.final:
                out.append(posixpath.relpath(c.plan.output_path, base or "."))
            else:
                out.append("not saved (see the report)")
        return out

    def prepare(self, ctx: TaskContext, work: WorkItem) -> LoJob | None:
        work.category = Category.EMAIL
        path = work.input_path()
        try:
            msg = parse(_read(path))
        except Unparseable as e:
            work.fail("CONVERSION_ERROR", f"This isn't an e-mail Baleen can read: {e}")
            return None
        work.source_format = source_format(msg)
        hide = bool(work.settings.get("hide_email_addresses", False))
        pol = str(work.settings.get("eml_attachments", "extract"))
        an = analyse(msg, hide=hide)
        doc = render(msg, saved=self._saved(work, an.attachments, pol), hide=hide, policy=pol, analysis=an)
        if an.problems and "CHARSET_ERRORS" not in work.reasons:
            work.reasons.append("CHARSET_ERRORS")
            work.messages.append(" ".join(an.problems))
        inp = work.out(f"mail-{work.n}.html")
        with open(long_path(inp), "wb") as f:
            f.write(b"\xef\xbb\xbf" + doc.encode("utf-8", "replace"))
        return LoJob(input_path=inp, out_dir=work.work_dir, export_filter="writer_pdf_Export",
                     pdfa_level=str(work.settings.get("pdfa_level", "2b")), infilter=self.infilter)

    def finish(self, ctx: TaskContext, work: WorkItem, result: LoResult) -> None:
        ok = pdf_finish(work, result)
        if result.method:
            work.method = self.METHOD_PREFIX + result.method
        if not ok:
            work.fail(result.reason or "CONVERSION_ERROR", result.message)

    def check_existing(self, ctx: TaskContext, work: WorkItem) -> None:
        check_existing_pdf(work)  # plan-time CHARSET_ERRORS stays on the item (§7.5 re-run keeps the flag)


ROUTE = register(EmailRoute())

