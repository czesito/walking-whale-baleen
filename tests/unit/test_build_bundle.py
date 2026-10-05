"""scripts/runtimes.json pins and scripts/build_bundle.py download verification (spec §14.6)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PINS = json.loads((REPO / "scripts" / "runtimes.json").read_text(encoding="utf-8"))
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _load_build_bundle():
    spec = importlib.util.spec_from_file_location("build_bundle", REPO / "scripts" / "build_bundle.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_bundle"] = mod
    spec.loader.exec_module(mod)
    return mod


bb = _load_build_bundle()


def _downloads():
    for key, comp in PINS["components"].items():
        for plat, entry in comp["platforms"].items():
            for dl in entry["downloads"]:
                yield key, plat, dl


def test_every_component_pinned_for_required_platforms():
    assert set(PINS["components"]) == set(bb.COMPONENTS)
    for key, comp in PINS["components"].items():
        for plat in ("win-x64", "mac-arm64"):  # DR-24; mac-x64 optional
            assert plat in comp["platforms"], f"{key} lacks {plat}"
        assert comp["license"] and comp["dest"]


@pytest.mark.parametrize(("key", "plat", "dl"), list(_downloads()), ids=lambda v: v if isinstance(v, str) else None)
def test_download_pins_are_complete(key, plat, dl):
    assert HEX64.match(dl["sha256"]), f"{key}/{plat}: sha256 must be 64 lower-case hex digits"
    assert dl["url"].startswith("https://")
    assert all(m.startswith("https://") for m in dl.get("mirrors", []))
    assert dl["checksum_source"] in ("vendor", "pinned-at-review")
    if dl["checksum_source"] == "vendor":
        assert dl.get("checksum_url", "").startswith("https://")
    assert "/" not in dl["file"] and "\\" not in dl["file"]
    if key == "libreoffice":
        assert dl["url"].startswith("https://downloadarchive.documentfoundation.org/")
    assert "latest" not in dl["url"].lower(), "pin a versioned URL, not a rolling one"


def test_cache_file_names_are_unique_per_content():
    seen: dict[str, str] = {}
    for _key, _plat, dl in _downloads():
        assert seen.setdefault(dl["file"], dl["sha256"]) == dl["sha256"], f"{dl['file']} pinned to two hashes"


def test_ffmpeg_build_pins():
    ff = PINS["components"]["ffmpeg"]
    assert ff["license"] == "GPL-2.0-or-later"
    srcs = ff["build"]["sources"]
    assert {"ffmpeg", "x264"} <= set(srcs)
    for name, s in srcs.items():
        assert HEX64.match(s["sha256"]), name
        assert "/" not in s["file"] and s["license"] and s["what"]
        assert s.get("url", "").startswith("https://") or all(u.startswith("https://") for u in s["git"])
    assert re.fullmatch(r"[0-9a-f]{40}", srcs["x264"]["commit"]) and srcs["x264"]["commit"] in srcs["x264"]["file"]
    assert srcs.get("zlib", {}).get("platforms", ["win-x64"]) == ["win-x64"]
    # third-party prebuilt binaries stay, but only for development
    assert all(e.get("dev_only") for e in ff["platforms"].values())


@pytest.mark.parametrize("plat", ["win-x64", "mac-arm64", "mac-x64"])
def test_build_sources_per_platform(plat):
    ctx = bb.Ctx(platform=plat, version="0", cache=REPO, runtime=REPO, work=REPO, pins=PINS)
    names = set(bb.build_sources(ctx))
    assert names == ({"ffmpeg", "x264", "zlib"} if plat == "win-x64" else {"ffmpeg", "x264"})


def test_build_script_configuration():
    script = (REPO / "scripts" / "build_ffmpeg.sh").read_text(encoding="utf-8")
    for flag in ("--enable-gpl", "--enable-libx264", "--enable-static", "--disable-shared", "--disable-network",
                 "--disable-doc", "--disable-ffplay", "--disable-autodetect"):
        assert flag in script
    configure = script.split("FF_CONFIGURE=(", 1)[1].split(")", 1)[0]
    assert "--enable-version3" not in configure and "nonfree" not in configure


def test_fetch_verifies_and_caches(tmp_path):
    payload = b"baleen pinned payload\n"
    src = tmp_path / "src.bin"
    src.write_bytes(payload)
    cache = tmp_path / "cache"
    cache.mkdir()
    dl = {"file": "payload.bin", "url": src.as_uri(), "sha256": hashlib.sha256(payload).hexdigest()}
    out = bb.fetch(dl, cache)
    assert out.read_bytes() == payload
    assert bb.fetch(dl, cache) == out  # second call uses the verified cache


def test_fetch_aborts_on_mismatch(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"tampered")
    cache = tmp_path / "cache"
    cache.mkdir()
    dl = {"file": "payload.bin", "url": src.as_uri(), "sha256": "0" * 64}
    with pytest.raises(bb.BuildError, match="SHA-256 mismatch"):
        bb.fetch(dl, cache)
    assert not (cache / "payload.bin").exists()
    assert not (cache / "payload.bin.part").exists()


def test_cached_file_mismatch_aborts(tmp_path):
    (tmp_path / "payload.bin").write_bytes(b"corrupt")
    dl = {"file": "payload.bin", "url": "https://invalid.example/payload.bin", "sha256": "1" * 64}
    with pytest.raises(bb.BuildError, match="cached"):
        bb.fetch(dl, tmp_path)


def test_verapdf_template_installs_cli_only():
    xml = (REPO / "scripts" / "verapdf-auto-install.xml").read_text(encoding="utf-8")
    assert "@INSTALL_PATH@" in xml
    assert re.search(r'name="veraPDF CLI" selected="true"', xml)
    assert re.search(r'name="veraPDF GUI" selected="false"', xml)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ANSI code page check")
def test_ansi_path_guard_rejects_unencodable(tmp_path):
    import ctypes

    cp = f"cp{ctypes.windll.kernel32.GetACP()}"
    bad = next((c for c in ("測試", "Ж", "\U0001f40b") if not _encodable(c, cp)), None)
    if bad is None:
        pytest.skip(f"every probe character is encodable in {cp}")
    with pytest.raises(bb.BuildError, match="R-02"):
        bb.check_ansi_path("--dest", tmp_path / bad)
    bb.check_ansi_path("--dest", tmp_path / "plain folder")


def test_check_ffmpeg_parses_listings():
    import importlib

    sys.path.insert(0, str(REPO / "scripts"))
    check = importlib.import_module("check_ffmpeg")
    listing = ("Decoders:\n V..... = Video\n ------\n V....D h264  H.264\n A....D aac   AAC\n"
               " D  mov,mp4,m4a  QuickTime / MOV\n")
    assert {"h264", "aac", "mov", "mp4", "m4a"} <= check.names(listing)


def test_launchers_and_readme():
    bat = (REPO / "launchers" / "Start Baleen.bat").read_bytes()
    bat.decode("ascii")  # cmd.exe reads the file in the console code page
    for needle in (b"chcp 65001", b"BALEEN_HOME", b"PYTHONNOUSERSITE=1", b"PYTHONDONTWRITEBYTECODE=1",
                   b"JAVA_TOOL_OPTIONS", b"-m baleen serve", b"pause"):
        assert needle in bat
    command = (REPO / "launchers" / "Start Baleen.command").read_bytes()
    assert command.startswith(b"#!/bin/sh\n") and b"\r" not in command
    assert b"TMPDIR=" in command and b"-m baleen serve" in command
    readme = (REPO / "launchers" / "README.txt").read_text(encoding="utf-8")
    for topic in ("Unblock", "SmartScreen", "xattr -dr com.apple.quarantine", "Uninstall", "Privacy"):
        assert topic in readme


def test_text_bytes_line_endings():
    assert bb.text_bytes("a\r\nb\nc", True) == b"a\r\nb\r\nc"
    assert bb.text_bytes("a\r\nb\nc", False) == b"a\nb\nc"


def test_dist_notice_includes_licence_files(tmp_path):
    site = tmp_path / "runtime" / "python" / "Lib" / "site-packages"
    dist = site / "demo_pkg-1.2.dist-info"
    (dist / "licenses").mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.4\nName: Demo_Pkg\nVersion: 1.2\n"
                                   "License-Expression: MIT\nProject-URL: Source, https://example.org/demo\n\nBody\n",
                                   encoding="utf-8")
    (dist / "licenses" / "LICENSE").write_text("MIT licence text\n", encoding="utf-8")
    ctx = bb.Ctx(platform="win-x64", version="0", cache=tmp_path, runtime=tmp_path / "runtime", work=tmp_path, pins={})
    name, text = bb._dist_notice(ctx, dist)
    assert name == "demo-pkg"
    assert "Licence:       MIT" in text and "MIT licence text" in text and "https://example.org/demo" in text
    baleen = site / "walking_whale_baleen-0.1.0.dist-info"
    baleen.mkdir()
    (baleen / "METADATA").write_text("Name: walking-whale-baleen\nVersion: 0.1.0\n", encoding="utf-8")
    assert bb._dist_notice(ctx, baleen) is None


def test_make_zip_layout_and_sums(tmp_path):
    bundle = tmp_path / "Baleen-9.9.9-win-x64"
    (bundle / "runtime" / "empty").mkdir(parents=True)
    (bundle / "README.txt").write_text("hello\n", encoding="utf-8")
    zip_path = tmp_path / "baleen-9.9.9-win-x64.zip"
    if sys.platform == "darwin":
        pytest.skip("macOS zips are made by ditto")
    digest = bb.make_zip(bundle, zip_path)
    import zipfile

    names = zipfile.ZipFile(zip_path).namelist()
    assert "Baleen-9.9.9-win-x64/README.txt" in names
    assert "Baleen-9.9.9-win-x64/runtime/empty/" in names
    assert (tmp_path / "SHA256SUMS").read_text(encoding="utf-8") == f"{digest}  {zip_path.name}\n"


def test_compare_expected(tmp_path):
    report = {
        "photos/photo.jpg": {"status": "OK", "reason": ""},
        "texts/notes.txt": {"status": "NEEDS_REVIEW", "reason": "B;A"},
        "other/Thumbs.db": {"status": "IGNORED", "reason": "SYSTEM_FILE"},
    }
    csv_path = tmp_path / "smoke-expected.csv"
    csv_path.write_text("# comment\nsource_path,status,reason\nphotos/photo.jpg,OK,\ntexts/notes.txt,NEEDS_REVIEW,A;B\n"
                        "other/Thumbs.db,IGNORED,SYSTEM_FILE\n", encoding="utf-8")
    assert bb.compare_expected(report, csv_path) == []
    csv_path.write_text("source_path,status,reason\nphotos/photo.jpg,FAILED,X\nmissing.txt,OK,\n"
                        "texts/notes.txt,NEEDS_REVIEW,A\n", encoding="utf-8")
    problems = bb.compare_expected(report, csv_path)
    assert any("photos/photo.jpg: status OK, expected FAILED" in p for p in problems)
    assert any("photos/photo.jpg: reason" in p for p in problems)
    assert any("missing.txt" in p for p in problems)
    assert any("texts/notes.txt: reason" in p for p in problems)
    assert any("other/Thumbs.db: in the report but not in" in p for p in problems)


def test_tree_state_ignores_data(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x").write_text("1", encoding="utf-8")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "y").write_text("2", encoding="utf-8")
    assert list(bb.tree_state(tmp_path)) == ["runtime/y"]


def _encodable(text: str, cp: str) -> bool:
    try:
        text.encode(cp)
    except UnicodeEncodeError:
        return False
    return True
