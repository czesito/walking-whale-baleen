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


def test_ffmpeg_gpl_sources_pinned():
    for plat, entry in PINS["components"]["ffmpeg"]["platforms"].items():
        whats = " ".join(s["what"] for s in entry["gpl_sources"])
        assert "FFmpeg" in whats, plat
        assert entry["x264"]["core"]
    win = PINS["components"]["ffmpeg"]["platforms"]["win-x64"]
    assert re.fullmatch(r"[0-9a-f]{40}", win["x264"]["commit"])


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


def _encodable(text: str, cp: str) -> bool:
    try:
        text.encode(cp)
    except UnicodeEncodeError:
        return False
    return True
