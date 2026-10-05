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


def test_network_and_local_references() -> None:
    from baleen.convert.sanitize import is_remote

    for url in (r"\\nas\share\a.png", "file://nas/share/a.png", r"file:\\nas\share\a.png", "//cdn/x.png",
                "HTTPS://x/y"):
        assert is_remote(url), url
    for url in ("file:///C:/x.png", "file://localhost/C:/x.png", "img/a.png", r"C:\x.png", "/x.png", "#top"):
        assert not is_remote(url), url
    page = r'<img src="file:///C:/Users/me/secret.png"><img src="C:\Users\me\b.png"><img src="ok.png">'
    out = sanitize_html(page, allow_local=False)
    assert "<img" in out and 'src="ok.png"' in out
    assert 'src="file:' not in out and r'src="C:\Users' not in out
    assert "not archived: file:///C:/Users/me/secret.png" in out
    out2 = sanitize_html(r'<img src="\\nas\share\x.png">')
    assert "<img" not in out2 and "not archived" in out2
    # local references stay for HTML pages (resolved by the HTML route inside the source root)
    assert 'src="file:///C:/x.png"' in sanitize_html('<img src="file:///C:/x.png">')
