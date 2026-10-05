"""Diagnose V-TEXT (TEXT_LOSS) for CJK text converted by LibreOffice on macOS (diag-mac branch only).

    python mac_text.py fonts FONTS.json            # summarise system_profiler -json SPFontsDataType
    python mac_text.py run RUNTIME OUT             # convert test texts, then analyse the PDFs

`run` uses the bundled runtime's Python (pypdf, baleen) and LibreOffice. Round 2: checks the
FONTCONFIG_FILE fix in tools.tool_env.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NOTES = "Synthetic smoke-test notes.\n訪談 — UTF-8 text.\n"
CJK = "訪談紀錄\n繁體中文的測試內容。\n舊檔案與新檔案。\n"
BIG5 = "舊檔案：繁體中文的 Big5 編碼文字。\n第二行，測試用的合成內容。\n"
FAMILIES = re.compile(r"pingfang|hiragino|heiti|songti|arial unicode|stheiti|lantinghei|apple sd gothic|"
                      r"lastresort|noto", re.I)


def fonts(path: str) -> None:
    data = json.load(open(path, encoding="utf-8"))
    rows = []
    for f in data.get("SPFontsDataType", []):
        if not FAMILIES.search(f.get("_name", "")):
            continue
        for face in f.get("typefaces", []):
            rows.append((face.get("family", ""), face.get("_name", ""), f.get("type", ""), f.get("path", "")))
    for r in sorted(set(rows)):
        print("  " + " | ".join(str(x) for x in r))


def sh(cmd: list, **kw) -> subprocess.CompletedProcess[str]:  # noqa: ANN003
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=900, **kw)


def resolve(obj):  # noqa: ANN001, ANN201
    return obj.get_object() if hasattr(obj, "get_object") else obj


def font_report(pdf: Path) -> None:
    from pypdf import PdfReader

    r = PdfReader(str(pdf), strict=False)
    seen = set()
    for pno, page in enumerate(r.pages):
        stack = [page.get("/Resources", {})]
        while stack:
            res = resolve(stack.pop())
            if not res:
                continue
            for name, ref in (resolve(res.get("/Font", {})) or {}).items():
                f = resolve(ref)
                key = (f.get("/BaseFont"), f.get("/Subtype"))
                if key in seen:
                    continue
                seen.add(key)
                desc_sub, ff = "", ""
                fd = f.get("/FontDescriptor")
                if f.get("/Subtype") == "/Type0":
                    d = resolve(resolve(f["/DescendantFonts"])[0])
                    desc_sub = d.get("/Subtype")
                    fd = d.get("/FontDescriptor")
                if fd is not None:
                    fd = resolve(fd)
                    for k in ("/FontFile", "/FontFile2", "/FontFile3"):
                        if k in fd:
                            ff = k + (f" {resolve(fd[k]).get('/Subtype')}" if k == "/FontFile3" else "")
                outline = {"/FontFile2": "TrueType", "/FontFile": "Type1"}.get(ff.split(" ")[0], "CFF" if ff else "?")
                tu = f.get("/ToUnicode")
                tu_info = "no ToUnicode"
                if tu is not None:
                    cmap = resolve(tu).get_data().decode("latin-1", "replace")
                    targets = re.findall(r"<([0-9A-Fa-f]{4,})>\s*$", cmap, re.M)
                    chars = "".join(chr(int(t[:4], 16)) for t in targets if len(t) >= 4)
                    tu_info = f"ToUnicode maps {len(targets)} glyphs, e.g. {chars[:30]!r}"
                print(f"    page {pno + 1} {name}: {f.get('/BaseFont')} {f.get('/Subtype')}/{desc_sub} "
                      f"{ff} -> {outline}; {tu_info}")
            for _x, xref in (resolve(res.get("/XObject", {})) or {}).items():
                x = resolve(xref)
                if x.get("/Subtype") == "/Form" and "/Resources" in x:
                    stack.append(x["/Resources"])


def text_report(pdf: Path, source: str) -> None:
    from baleen.verify.pdf import extract_text, v_text, visible_chars

    print(f"    pypdf text: {extract_text(str(pdf))!r}")
    res = v_text(str(pdf), visible_chars(source))
    print(f"    V-TEXT: {res.state.value if hasattr(res.state, 'value') else res.state} {res.message}")
    if shutil.which("pdffonts"):
        print("    pdffonts:\n" + "\n".join("      " + ln for ln in sh(["pdffonts", str(pdf)]).stdout.splitlines()))


def run(runtime: str, out: str) -> None:
    rt, base = Path(runtime), Path(out)
    shutil.rmtree(base, ignore_errors=True)
    py = sys.executable
    app = rt / "LibreOffice.app"
    soffice = app / "Contents" / "MacOS" / "soffice"
    (base / "src").mkdir(parents=True)
    (base / "src" / "notes.txt").write_bytes(NOTES.encode("utf-8"))
    (base / "src" / "cjk.txt").write_bytes(CJK.encode("utf-8"))
    (base / "src-big5").mkdir()
    (base / "src-big5" / "big5.txt").write_bytes(BIG5.encode("big5"))
    texts = {"notes": NOTES, "cjk": CJK, "big5": BIG5}

    print("\n== does LibreOffice's launcher override fontconfig?")
    confs = sorted(str(p.relative_to(app)) for p in app.rglob("*.conf"))
    print(f"  *.conf inside the .app: {confs[:20]}")
    font_dirs = sorted({str(p.parent.relative_to(app)) for p in app.rglob("*.[ot]t[fc]")})
    print(f"  font folders inside the .app: {font_dirs}")
    for lib in sorted(app.rglob("*.dylib")) + [soffice]:
        try:
            data = lib.read_bytes()
        except OSError:
            continue
        hits = [s for s in (b"FONTCONFIG_FILE", b"FONTCONFIG_PATH", b"SAL_FONTPATH", b"fonts.conf") if s in data]
        if hits:
            print(f"  {lib.relative_to(app)}: {[h.decode() for h in hits]}")

    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "BALEEN_", "FONTCONFIG", "FC_"))}
    env.update(BALEEN_HOME=str(base / "home"), BALEEN_RUNTIME=str(rt))

    print("\n== the generated config (tools.tool_env)")
    from baleen.home import Home
    from baleen.tools import tool_env

    os.environ["BALEEN_RUNTIME"] = str(rt)
    tenv = tool_env(Home(base / "home"))
    conf = Path(tenv["FONTCONFIG_FILE"])
    print(conf.read_text(encoding="utf-8"))

    print("\n== soffice with FC_DEBUG=1024 (which config files fontconfig loads)")
    outdir = base / "fcdebug"
    outdir.mkdir()
    p = sh([soffice, f"-env:UserInstallation={(base / 'prof-fc').as_uri()}", "--headless", "--norestore",
            "--infilter=Text (encoded):UTF8,LF,,,", "--convert-to", "pdf", "--outdir", outdir, base / "src" / "cjk.txt"],
           env={**tenv, "FC_DEBUG": "1024"})
    lines = [ln for ln in (p.stdout + p.stderr).splitlines() if "onfig" in ln or "cache" in ln.lower()]
    print("\n".join("  " + ln for ln in lines[:30]))
    for pdf in outdir.glob("*.pdf"):
        font_report(pdf)

    print("\n== baleen convert (as the bundle does), PDF/A-2b and PDF/A-1b")
    runs = (("src", "out-2b", []), ("src-big5", "out-big5", ["--set", "txt_encoding=big5"]),
            ("src", "out-1b", ["--set", "pdfa_level=1b"]))
    for src, outdir, extra in runs:
        p = sh([py, "-m", "baleen", "convert", base / src, base / outdir, "--quiet", *extra], env=env)
        print(f"  exit {p.returncode} {p.stderr[-600:]}")
        for rep in sorted((base / outdir / "_baleen").glob("report-*.csv")):
            for row in csv.DictReader(open(rep, encoding="utf-8-sig")):
                print(f"  [{outdir}] {row['source_path']}: {row['status']} {row['reason']} | {row['checks']} | "
                      f"{row['message'][:400]}")
        for pdf in sorted((base / outdir).rglob("*.pdf")):
            print(f"\n  -- {pdf.relative_to(base)}")
            font_report(pdf)
            text_report(pdf, texts[pdf.stem])
    cache = base / "home" / "data" / "fontconfig" / "cache"
    print(f"\nfontconfig cache files in data/: {len(list(cache.glob('*')))}")


if __name__ == "__main__":
    if sys.argv[1] == "fonts":
        fonts(sys.argv[2])
    else:
        run(sys.argv[2], sys.argv[3])
