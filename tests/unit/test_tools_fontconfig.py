"""macOS fontconfig config for headless LibreOffice (tools.ensure_fontconfig, DR-15)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from baleen import tools
from baleen.home import Home


def _home(tmp_path: Path, monkeypatch) -> Home:  # noqa: ANN001
    monkeypatch.delenv("BALEEN_RUNTIME", raising=False)
    home = Home(tmp_path / "Baleen & co")
    res = home.runtime_dir / "LibreOffice.app" / "Contents" / "Resources"
    (res / "fonts").mkdir(parents=True)
    (res / "resource" / "common" / "fonts").mkdir(parents=True)
    return home


def test_config_lists_libreoffice_and_system_folders(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "_MAC_ASSET_FONT_GLOB", str(tmp_path / "assets" / "com_apple_MobileAsset_Font*"))
    (tmp_path / "assets" / "com_apple_MobileAsset_Font7").mkdir(parents=True)
    conf = tools.ensure_fontconfig(home)
    assert conf == home.data_dir / "fontconfig" / "fonts.conf"
    root = ET.parse(conf).getroot()  # well-formed despite '&' in the path
    dirs = [d.text for d in root.findall("dir")]
    res = home.runtime_dir / "LibreOffice.app" / "Contents" / "Resources"
    assert dirs[:2] == [str(res / "fonts"), str(res / "resource" / "common" / "fonts")]
    for d in ("/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
              str(Path.home() / "Library" / "Fonts"), str(tmp_path / "assets" / "com_apple_MobileAsset_Font7")):
        assert d in dirs
    cache = root.find("cachedir").text
    assert cache == str(home.data_dir / "fontconfig" / "cache") and Path(cache).is_dir()  # AC-11: inside data/
    fallback = [s.text for s in root.findall("match/edit/string")]
    assert fallback[0] == "PingFang TC" and "Arial Unicode MS" in fallback


def test_config_is_rewritten_only_when_it_changes(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    conf = tools.ensure_fontconfig(home)
    mtime = conf.stat().st_mtime_ns
    assert tools.ensure_fontconfig(home) == conf and conf.stat().st_mtime_ns == mtime
    conf.write_text("stale", encoding="utf-8")
    tools.ensure_fontconfig(home)
    assert conf.read_text(encoding="utf-8").startswith("<?xml")


def test_tool_env_sets_fontconfig_file_on_macos_only(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "IS_MAC", True)
    assert tools.tool_env(home)["FONTCONFIG_FILE"] == str(home.data_dir / "fontconfig" / "fonts.conf")
    monkeypatch.setattr(tools, "IS_MAC", False)
    assert "FONTCONFIG_FILE" not in tools.tool_env(home)
