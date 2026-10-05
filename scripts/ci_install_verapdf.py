"""Install the pinned veraPDF CLI with the system Java (CI on Linux, spec §14.7).

    python scripts/ci_install_verapdf.py --dest DIR [--java PATH] [--cache DIR]

Uses the same pinned installer (SHA-256 checked) and auto-install template as build_bundle.py and
prints the path of the veraPDF launcher, for BALEEN_VERAPDF.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_bundle as bb  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, required=True, help="folder to install veraPDF into (must not exist)")
    ap.add_argument("--java", default=shutil.which("java") or "java", help="java executable (default: from PATH)")
    ap.add_argument("--cache", type=Path, default=bb.REPO / ".scratch" / "cache")
    ns = ap.parse_args(argv)

    comp = json.loads(bb.RUNTIMES_JSON.read_text(encoding="utf-8"))["components"]["verapdf"]
    entry = comp["platforms"]["mac-arm64" if sys.platform == "darwin" else "win-x64"]  # same installer zip
    dest = ns.dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} is not empty", file=sys.stderr)
        return 1
    ns.cache.mkdir(parents=True, exist_ok=True)
    archive = bb.fetch(entry["downloads"][0], ns.cache.resolve())
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        bb.extract(archive, staging / "x")
        jar = bb.find_one(staging / "x", "verapdf-izpack-installer-*.jar", "veraPDF installer")
        template = (bb.REPO / entry["install"]["template"]).read_text(encoding="utf-8")
        auto = staging / "auto-install.xml"
        auto.write_text(template.replace("@INSTALL_PATH@", xml_escape(str(dest))), encoding="utf-8")
        bb.run([ns.java, "-Djava.awt.headless=true", f"-Djava.io.tmpdir={tmp}", "-jar", jar, auto], timeout=1800)
    launcher = dest / ("verapdf.bat" if sys.platform == "win32" else "verapdf")
    if not launcher.is_file():
        print(f"installer did not create {launcher}", file=sys.stderr)
        return 1
    if sys.platform != "win32":
        launcher.chmod(0o755)
    print(launcher)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
