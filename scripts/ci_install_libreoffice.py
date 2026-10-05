"""Install the pinned LibreOffice for Linux CI without root (spec §14.7).

    python scripts/ci_install_libreoffice.py --dest DIR [--cache DIR]

CI must test the LibreOffice release the bundles ship (26.8.0.3), not the distribution's older
build: Ubuntu's 24.2 writes PDF/A-1b files that veraPDF rejects (rules 6.7.3 and 6.3.5).
The official .deb set is unpacked with `dpkg -x` (nothing is installed system-wide) and the
path of `soffice` is printed, for BALEEN_SOFFICE.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_bundle as bb  # noqa: E402

VERSION = "26.8.0.3"
DOWNLOAD = {
    "file": f"LibreOffice_{VERSION}_Linux_x86-64_deb.tar.gz",
    "url": f"https://downloadarchive.documentfoundation.org/libreoffice/old/{VERSION}/deb/x86_64/"
           f"LibreOffice_{VERSION}_Linux_x86-64_deb.tar.gz",
    # Vendor checksum: <url>.sha256 on downloadarchive.documentfoundation.org
    "sha256": "d0a6031a3837e48f9854e6d2da6489b9fadbd814afa4741fa32a197741663a22",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=bb.REPO / ".scratch" / "cache")
    ns = ap.parse_args(argv)
    dest = ns.dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    ns.cache.mkdir(parents=True, exist_ok=True)
    archive = bb.fetch(DOWNLOAD, ns.cache.resolve())
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive) as tf:
            tf.extractall(tmp, filter="data")
        debs = sorted(Path(tmp).rglob("*.deb"))
        if not debs:
            print("no .deb files in the archive", file=sys.stderr)
            return 1
        for deb in debs:
            subprocess.run(["dpkg", "-x", str(deb), str(dest)], check=True)
    hits = sorted(dest.glob("opt/libreoffice*/program/soffice"))
    if not hits:
        print("soffice not found after unpacking", file=sys.stderr)
        return 1
    print(hits[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
