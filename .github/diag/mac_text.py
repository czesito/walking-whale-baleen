"""Diagnose V-TEXT (TEXT_LOSS) for CJK text converted by LibreOffice on macOS (diag-mac branch only).

    python mac_text.py fonts FONTS.json            # summarise system_profiler -json SPFontsDataType
    python mac_text.py run RUNTIME OUT             # convert test texts, then analyse the PDFs

`run` uses the bundled runtime's Python (pypdf, baleen) and LibreOffice.
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
                      r"lastresort|helvetica|times|noto", re.I)


def fonts(path: str) -> None:
    data = json.load(open(path, encoding="utf-8"))
    rows = []
    for f in data.get("SPFontsDataType", []):
        if not FAMILIES.search(f.get("_name", "")):
            continue
        for face in f.get("typefaces", []):
            rows.append((face.get("family", ""), face.get("_name", ""), f.get("type", ""), f.get("path", ""),
                         face.get("enabled", ""), face.get("valid", "")))
    for r in sorted(set(rows)):
        print("  " + " | ".join(str(x) for x in r))


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:  # noqa: ANN003
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600, **kw)


def resolve(obj):  # noqa: ANN001, ANN201
    return obj.get_object() if hasattr(obj, "get_object") else obj


def font_report(pdf: Path) -> None:
    from pypdf import PdfReader

    r = PdfReader(str(pdf), strict=False)
    seen = set()
    for pno, page in enumerate(r.pages):
        res = resolve(page.get("/Resources", {}))
        stack = [res]
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
                tu = f.get("/ToUnicode")
                tu_info = "no ToUnicode"
                if tu is not None:
                    cmap = resolve(tu).get_data().decode("latin-1", "replace")
                    targets = re.findall(r"<([0-9A-Fa-f]{4,})>\s*$", cmap, re.M)
                    chars = "".join(chr(int(t[:4], 16)) for t in targets if len(t) >= 4)
                    tu_info = (f"ToUnicode: {cmap.count('beginbfchar')} bfchar blocks, "
                               f"{cmap.count('beginbfrange')} bfrange blocks; maps to e.g. {chars[:40]!r}")
                print(f"    page {pno + 1} font {name}: BaseFont={f.get('/BaseFont')} Subtype={f.get('/Subtype')} "
                      f"Descendant={desc_sub} Encoding={f.get('/Encoding')} file={ff} {tu_info}")
            for _xname, xref in (resolve(res.get("/XObject", {})) or {}).items():
                x = resolve(xref)
                if x.get("/Subtype") == "/Form" and "/Resources" in x:
                    stack.append(x["/Resources"])


def text_report(pdf: Path, source: str) -> None:
    from baleen.verify.pdf import extract_text, v_text, visible_chars

    got = extract_text(str(pdf))
    print(f"    pypdf text: {got!r}")
    res = v_text(str(pdf), visible_chars(source))
    print(f"    V-TEXT: {res.state} {res.message}")
    if shutil.which("pdftotext"):
        p = sh(["pdftotext", "-enc", "UTF-8", str(pdf), "-"])
        print(f"    pdftotext: {p.stdout!r}")
    if shutil.which("pdffonts"):
        print("    pdffonts:\n" + "\n".join("      " + ln for ln in sh(["pdffonts", str(pdf)]).stdout.splitlines()))


def run(runtime: str, out: str) -> None:
    rt, base = Path(runtime), Path(out)
    shutil.rmtree(base, ignore_errors=True)
    py = sys.executable
    soffice = rt / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"
    sources = {"notes.txt": NOTES.encode("utf-8"), "cjk.txt": CJK.encode("utf-8")}
    (base / "src").mkdir(parents=True)
    for n, b in sources.items():
        (base / "src" / n).write_bytes(b)
    (base / "src-big5").mkdir()
    (base / "src-big5" / "big5.txt").write_bytes(BIG5.encode("big5"))
    texts = {"notes": NOTES, "cjk": CJK, "big5": BIG5}

    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "BALEEN_"))}
    env.update(BALEEN_HOME=str(base / "home"), BALEEN_RUNTIME=str(rt))
    print("\n== baleen convert (as the bundle does)")
    for src, outdir, extra in (("src", "out", []), ("src-big5", "out-big5", ["--set", "txt_encoding=big5"])):
        p = sh([py, "-m", "baleen", "convert", base / src, base / outdir, "--quiet", *extra], env=env)
        print(f"  exit {p.returncode} {p.stderr[-800:]}")
        for rep in sorted((base / outdir / "_baleen").glob("report-*.csv")):
            for row in csv.DictReader(open(rep, encoding="utf-8-sig")):
                print(f"  {row['source_path']}: {row['status']} {row['reason']} | {row['checks']} | {row['message'][:300]}")
        for pdf in sorted((base / outdir).rglob("*.pdf")):
            print(f"\n  -- {pdf.relative_to(base)}")
            font_report(pdf)
            text_report(pdf, texts[pdf.stem])

    print("\n== soffice variants on notes.txt and cjk.txt")
    pdfa = 'pdf:writer_pdf_Export:{"SelectPdfVersion":{"type":"long","value":"2"}}'
    variants = [
        ("default-pdfa", "Text (encoded):UTF8,LF,,,", pdfa),
        ("default-plainpdf", "Text (encoded):UTF8,LF,,,", "pdf"),
        ("lang-zhTW", "Text (encoded):UTF8,LF,,zh-TW,", pdfa),
        ("font-PingFangTC", "Text (encoded):UTF8,LF,PingFang TC,zh-TW,", pdfa),
        ("font-HeitiTC", "Text (encoded):UTF8,LF,Heiti TC,zh-TW,", pdfa),
        ("font-ArialUnicode", "Text (encoded):UTF8,LF,Arial Unicode MS,zh-TW,", pdfa),
    ]
    for label, infilter, conv in variants:
        outdir = base / "variants" / label
        outdir.mkdir(parents=True)
        prof = (base / "profiles" / label).as_uri()
        p = sh([soffice, f"-env:UserInstallation={prof}", "--headless", "--norestore", "--nologo",
                f"--infilter={infilter}", "--convert-to", conv, "--outdir", outdir,
                base / "src" / "notes.txt", base / "src" / "cjk.txt"], env={k: v for k, v in env.items()})
        print(f"  [{label}] exit {p.returncode} {p.stderr.strip()[-300:]}")
        for pdf in sorted(outdir.glob("*.pdf")):
            print(f"\n  -- {label}/{pdf.name}")
            font_report(pdf)
            text_report(pdf, texts[pdf.stem])


if __name__ == "__main__":
    if sys.argv[1] == "fonts":
        fonts(sys.argv[2])
    else:
        run(sys.argv[2], sys.argv[3])
