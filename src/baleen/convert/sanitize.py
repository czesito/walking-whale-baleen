"""HTML sanitising before LibreOffice import (spec §6.4, P8, SEC-9, R-05).

- Remove <script>, <iframe>, <object>, <embed> (and <frame>, <applet>, meta refresh).
- Remote resource URLs (http:, https:, ftp:, //) in src, srcset, background, poster,
  <link href> and CSS url()/@import are replaced by visible text
  "[external resource not archived: URL]".
- Anchor links (<a href>) stay as they are.
- Relative resources resolve against the original source folder through an injected
  <base href> (read-only access; nothing is created there).

The output is a complete HTML document string. Used by the HTML route and the e-mail route.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from html.parser import HTMLParser

# Remote = fetched over a network: http(s), ftp, protocol-relative //, UNC \\host\share,
# and file://host/... with a host other than localhost (SMB on Windows).
REMOTE = re.compile(
    r"^\s*(?:https?:|ftp:|//|\\\\|file:(?://|\\\\)(?!localhost[/\\]|/|\\)[^/\\\s]+)", re.I
)
# Absolute local references: file:..., C:\ or C:/, and root paths.
LOCAL_ABS = re.compile(r"^\s*(?:file:|[a-z]:[\\/]|/)", re.I)
CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
CSS_IMPORT = re.compile(r"@import\s+(?:url\()?\s*(['\"]?)([^'\")\s;]+)\1\s*\)?[^;]*;?", re.I)

DROP_WITH_CONTENT = {"script", "iframe", "object", "applet", "noscript", "frameset"}
DROP_VOID = {"embed", "frame", "base"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
        "track", "wbr", "frame"}
URL_ATTRS = {"src", "background", "poster", "data", "lowsrc", "dynsrc", "longdesc", "codebase", "action",
             "formaction", "manifest", "icon", "profile", "cite"}


def placeholder(url: str) -> str:
    return f"[external resource not archived: {url.strip()}]"


def is_remote(url: str | None) -> bool:
    """True for URLs that would be fetched over a network (see REMOTE)."""
    return bool(url) and bool(REMOTE.match(url or ""))


def blocker(allow_local: bool) -> Callable[[str | None], bool]:
    """Predicate for references to neutralise: remote ones always; absolute local ones when not allowed."""

    def blocked(url: str | None) -> bool:
        if is_remote(url):
            return True
        return not allow_local and bool(url) and bool(LOCAL_ABS.match(url or ""))

    return blocked


def _clean_css(css: str, notes: list[str], blocked: Callable[[str | None], bool] = is_remote) -> str:
    def imp(m: re.Match[str]) -> str:
        if blocked(m.group(2)):
            notes.append(m.group(2))
            return ""
        return m.group(0)

    def url(m: re.Match[str]) -> str:
        if blocked(m.group(2)):
            notes.append(m.group(2))
            return "none"
        return m.group(0)

    return CSS_URL.sub(url, CSS_IMPORT.sub(imp, css))


class _Sanitiser(HTMLParser):
    def __init__(self, allow_local: bool = True) -> None:
        super().__init__(convert_charrefs=False)
        self.blocked = blocker(allow_local)
        self.out: list[str] = []
        self.skip_depth = 0
        self.skip_tag = ""
        self.head_notes: list[str] = []  # remote resources found where text can't be shown
        self.in_style = False
        self.saw_head = False
        self.saw_body = False
        self.body_index: int | None = None

    # -- helpers
    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> tuple[str, list[str]]:
        notes: list[str] = []
        kept: list[str] = []
        for k, v in attrs:
            k = k.lower()
            if k.startswith("on"):
                continue  # event handlers are scripts
            if v is None:
                kept.append(k)
                continue
            if k == "href" and tag == "a":
                if v.strip().lower().startswith("javascript:"):
                    continue
                kept.append(f'{k}="{html.escape(v, quote=True)}"')
                continue
            if k == "href" and tag in ("link",) and self.blocked(v):
                notes.append(v)
                continue
            if k in URL_ATTRS and self.blocked(v):
                notes.append(v)
                continue
            if k == "srcset":
                cands = [c.strip() for c in v.split(",") if c.strip()]
                local = [c for c in cands if not self.blocked(c.split()[0])]
                notes += [c.split()[0] for c in cands if self.blocked(c.split()[0])]
                if local:
                    kept.append(f'srcset="{html.escape(", ".join(local), quote=True)}"')
                continue
            if k == "style":
                v = _clean_css(v, notes, self.blocked)
            if v.strip().lower().startswith(("javascript:", "vbscript:")):
                continue
            kept.append(f'{k}="{html.escape(v, quote=True)}"')
        return (" " + " ".join(kept)) if kept else "", notes

    def _emit_notes(self, notes: list[str]) -> None:
        for n in notes:
            self.out.append(f"<span>{html.escape(placeholder(n))}</span>")

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip_depth, self.skip_tag = 1, tag
            return
        if tag in DROP_VOID:
            return
        if tag == "meta" and any(k.lower() == "http-equiv" and (v or "").lower() == "refresh" for k, v in attrs):
            return
        if tag == "head":
            self.saw_head = True
        a, notes = self._attrs(tag, attrs)
        if tag in ("img", "input", "video", "audio", "source", "track") and notes and not any(
                k.lower() in ("src", "srcset") and not self.blocked(v) for k, v in attrs if v):
            # The element itself only shows the remote resource: replace it by the text.
            self._emit_notes(notes)
            return
        if tag == "link" and notes:
            self.head_notes += notes
            return
        self.out.append(f"<{tag}{a}>")
        if tag == "body":
            self.saw_body = True
            self.body_index = len(self.out)
        if tag == "style":
            self.in_style = True
        if notes:
            if tag in ("head", "html", "meta", "style", "title"):
                self.head_notes += notes
            else:
                self._emit_notes(notes)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        if tag in DROP_WITH_CONTENT or tag in DROP_VOID or tag in VOID:
            return
        if tag == "style":
            self.in_style = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_style:
            data = _clean_css(data, self.head_notes, self.blocked)
        self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self.skip_depth:
            self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.skip_depth:
            self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return  # comments may hide conditional (IE) resource loads; drop them

    def handle_decl(self, decl: str) -> None:
        if not self.skip_depth:
            self.out.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        return


def sanitize_html(markup: str, *, base_href: str | None = None, title: str | None = None,
                  allow_local: bool = True) -> str:
    r"""Return a sanitised HTML document. `base_href` is a file URI of the source folder (ending '/').

    allow_local=False (e-mail bodies): absolute local references (file:, C:\, /) become placeholder
    text too, so a message can never pull files from this computer into its PDF.
    """
    p = _Sanitiser(allow_local)
    p.feed(markup)
    p.close()
    return _assemble(p, base_href, title)


def _assemble(p: _Sanitiser, base_href: str | None, title: str | None) -> str:
    body = "".join(p.out)
    notes_html = ""
    if p.head_notes:
        items = "".join(f"<div>{html.escape(placeholder(n))}</div>" for n in dict.fromkeys(p.head_notes))
        notes_html = f"<div>{items}</div>"
    base = f'<base href="{html.escape(base_href, quote=True)}">' if base_href else ""
    meta = '<meta charset="utf-8">'
    if p.saw_body and p.body_index is not None:
        out = p.out[: p.body_index] + [notes_html] + p.out[p.body_index:]
        doc = "".join(out)
    else:
        doc = f"<body>{notes_html}{body}</body>"
    if re.search(r"<head[\s>]", doc, re.I):
        doc = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + meta + base, doc, count=1, flags=re.I)
    else:
        t = f"<title>{html.escape(title)}</title>" if title else ""
        if re.search(r"<html[\s>]", doc, re.I):
            doc = re.sub(r"(<html[^>]*>)", lambda m: m.group(1) + f"<head>{meta}{base}{t}</head>", doc, count=1,
                         flags=re.I)
        else:
            doc = f"<html><head>{meta}{base}{t}</head>{doc}</html>"
    if not doc.lstrip().lower().startswith("<!doctype"):
        doc = "<!DOCTYPE html>\n" + doc
    return doc


def remote_urls_left(markup: str) -> list[str]:
    """For tests: remote URLs that could still be fetched (src/srcset/background/link/CSS)."""
    found = []
    for m in re.finditer(r"""(?:src|srcset|background|poster|data)\s*=\s*["']?\s*((?:https?:|ftp:|//)[^"'\s>]+)""",
                         markup, re.I):
        found.append(m.group(1))
    for m in re.finditer(r"<link[^>]+href\s*=\s*[\"']?\s*((?:https?:|ftp:|//)[^\"'\s>]+)", markup, re.I):
        found.append(m.group(1))
    for m in CSS_URL.finditer(markup):
        if is_remote(m.group(2)):
            found.append(m.group(2))
    return found
