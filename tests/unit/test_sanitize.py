"""§6.4 HTML sanitising: no remote fetches survive, anchors stay, <base href> injected."""

from __future__ import annotations

from baleen.convert.sanitize import placeholder, remote_urls_left, sanitize_html

PAGE = """<!DOCTYPE html><html><head><title>t</title>
<link rel="stylesheet" href="https://cdn.example.invalid/x.css">
<style>body{background:url('http://tracker.example.invalid/bg.png')} @import url("//evil.invalid/a.css");</style>
<script>fetch('https://example.invalid')</script>
<meta http-equiv="refresh" content="0;url=https://example.invalid">
</head><body background="http://example.invalid/bg.jpg" onload="alert(1)">
<p>Hello <a href="https://example.invalid/page">link stays</a></p>
<img src="http://example.invalid/pixel.gif" alt="pixel">
<img src="local.png" srcset="local@2x.png 2x, https://cdn.example.invalid/x@3x.png 3x">
<iframe src="https://example.invalid/frame"><p>inside iframe</p></iframe>
<object data="https://example.invalid/o.swf"></object><embed src="https://example.invalid/e.swf">
<div style="background-image:url(ftp://example.invalid/i.png)">styled</div>
<!--[if IE]><img src="http://example.invalid/ie.gif"><![endif]-->
</body></html>"""


def test_no_remote_resources_survive() -> None:
    out = sanitize_html(PAGE, base_href="file:///C:/src/folder/")
    assert remote_urls_left(out) == []
    assert "<script" not in out.lower() and "<iframe" not in out.lower() and "inside iframe" not in out
    assert "<object" not in out.lower() and "<embed" not in out.lower()
    assert "onload" not in out.lower() and "refresh" not in out.lower()
    assert '<a href="https://example.invalid/page">link stays</a>' in out
    assert placeholder("http://example.invalid/pixel.gif") in out
    assert placeholder("https://cdn.example.invalid/x.css") in out
    assert 'src="local.png"' in out and 'srcset="local@2x.png 2x"' in out
    assert '<base href="file:///C:/src/folder/">' in out
    assert "<!--" not in out


def test_fragment_gets_a_document() -> None:
    out = sanitize_html("<p>Just a fragment</p>", title="Mail")
    assert out.startswith("<!DOCTYPE html>") and "<title>Mail</title>" in out and "<p>Just a fragment</p>" in out
    assert '<meta charset="utf-8">' in out


def test_entities_and_cjk_preserved() -> None:
    out = sanitize_html("<p>訪談 &amp; notes &#169;</p>")
    assert "訪談 &amp; notes &#169;" in out
