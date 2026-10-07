"""--execute 流程（規格 §8）。重要原則：
  * 本模組沒有任何刪除來源檔案的程式碼路徑。
  * 只在 staging 與 *.part 暫存檔（本工具自己建立的）上做清理。
  * 每個檔案獨立；任何一步失敗（轉檔、驗證、複製）→ 該檔停止，原始檔不移動。
  * 原始檔只在「輸出已驗證並正式命名」之後，才被『搬移』到 <root>\\_ORIGINALS（保留相對路徑與原檔名）。"""
import csv
import hashlib
import os
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from . import config as C
from . import convert as CV
from . import validate as V
from .manifest import read_manifest
from .eml_info import enrich_eml
from .planner import A_CONVERT, A_COPY, A_EXT_NORM, A_VERIFY, classify, manifest_digest, plan
from .scanner import scan_fs
from .tools import detect, missing_for, pdfa_only_missing

LOG_COLUMNS = ["timestamp", "source_path", "target_path", "source_format", "target_format",
               "conversion_status", "rename_status", "verification_status", "error",
               "action", "target_sha256", "target_size", "archived_path", "original_relative_path"]


def sha256(path: str, buf=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                return h.hexdigest()
            h.update(b)


class Log:
    def __init__(self, path):
        new = not os.path.exists(path)
        self.f = open(path, "a", encoding="utf-8-sig", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=LOG_COLUMNS)
        if new:
            self.w.writeheader()

    def write(self, **kw):
        row = {c: "" for c in LOG_COLUMNS}
        row.update(kw)
        row["timestamp"] = datetime.now().isoformat(timespec="seconds")
        self.w.writerow(row)
        self.f.flush()
        os.fsync(self.f.fileno())


def _validate(path: str, tgt_ext: str, tools, src_dur=None):
    if tgt_ext in (".jpg", ".tif"):
        return V.validate_image(path, tgt_ext)
    if tgt_ext == ".pdf":
        return V.validate_pdfa(path, tools)
    if tgt_ext == ".mp4":
        return V.validate_mp4(path, tools, src_dur)
    return V.FAIL, f"unexpected target ext {tgt_ext}"


def _two_step_case_rename(src: str, dst: str):
    tmp = src + f".casetmp-{uuid.uuid4().hex[:8]}"
    os.rename(src, tmp)
    try:
        os.rename(tmp, dst)
    except OSError:
        os.rename(tmp, src)      # 回復
        raise


def run_execute(root: str, approved_digest_path: str, out_dir: str, staging: str,
                originals: str = "archive", archive_root: Optional[str] = None) -> int:
    tools = detect()
    # Step 1-3：重新掃描、重新規劃、核對 manifest digest
    files = scan_fs(root, check_readable=True)
    rows = plan(files, root)
    enrich_eml(rows)                       # 與 dry-run 相同：填入 attachment_*，有附件 → REVIEW_REQUIRED
    digest = manifest_digest(rows)
    approved = open(approved_digest_path, encoding="utf-8").read().split()[0]
    if digest != approved:
        raise SystemExit(f"ABORT: 目前重新規劃的 manifest digest ({digest[:12]}…) 與已核准的 ({approved[:12]}…) 不同。"
                         "來源資料夾可能已變動；請重新 dry-run 並重新確認。")
    os.makedirs(staging, exist_ok=True)
    log = Log(os.path.join(out_dir, "rename_conversion_log.csv"))
    ok = fail = skipped = 0
    for r in rows:
        if r.status not in (C.ST_PLANNED, C.ST_CONV_PLANNED, C.ST_ALREADY):
            skipped += 1
            log.write(source_path=r.source_path, target_path=r.target_path, source_format=r.source_extension,
                      target_format=r.target_extension, conversion_status="SKIPPED", rename_status="SKIPPED",
                      verification_status="", error=f"{r.status}: {r.reason}")
            continue
        try:
            res = _process(r, tools, staging, originals,
                           archive_root or (root.rstrip("\\/") + os.sep + C.ARCHIVE_DIRNAME), root)
        except Exception as e:  # noqa — 任何未預期錯誤：該檔停止，來源不動
            res = dict(conversion_status="ERROR", rename_status="NOT_DONE", verification_status="",
                       error=f"{type(e).__name__}: {e}", action="")
        log.write(source_path=r.source_path, target_path=r.target_path, source_format=r.source_extension,
                  target_format=r.target_extension, original_relative_path=r.source_relative_path, **res)
        if res.get("rename_status") in ("COPIED", "RENAMED_IN_PLACE", "ALREADY_VALID_VERIFIED", "ARCHIVED"):
            ok += 1
        else:
            fail += 1
    summary = os.path.join(out_dir, "rename_conversion_summary.txt")
    with open(summary, "w", encoding="utf-8") as f:
        f.write(f"finished {datetime.now().isoformat(timespec='seconds')}\nsucceeded={ok} failed={fail} skipped={skipped}\n"
                f"originals mode={originals} (archive = moved to <root>\\{C.ARCHIVE_DIRNAME} after validated output; never deleted)\n")
    return 0 if fail == 0 else 2


def _pdfa_verdict(st: str, msg: str):
    """把 validate_pdfa 的結果轉成對外的狀態字串。沒有 veraPDF 絕不寫成 verified。"""
    if st == V.OK:
        return f"PDF_A_VERIFIED: {msg}"
    if st == V.UNAVAILABLE:
        return f"{C.ST_PDFA_NOT_VALIDATED}: {msg}"
    return f"PDF_A_VALIDATION_FAILED: {msg}"


def _process(r, tools, staging, originals, archive_root, root) -> Dict:
    src, tgt = r.source_path, r.target_path
    _, tgt_ext, _, kind = classify(r.source_extension)
    # 來源仍存在且大小未變
    if not os.path.isfile(src) or os.path.getsize(src) != r.source_size:
        return dict(conversion_status="NOT_DONE", rename_status="NOT_DONE", error="SOURCE_CHANGED_OR_MISSING")
    miss = [m for m in missing_for(kind, tools) if not (m == "verapdf" and pdfa_only_missing(kind, tools))]
    if miss:
        return dict(conversion_status=C.ST_CONV_REQUIRED, rename_status="NOT_DONE", error="missing tools: " + ",".join(miss))

    # ---- 只驗證（既有 DP2 命名且副檔名已是目標；含 DP2_99_04_00_## 的 PDF）----
    if r.planned_action == A_VERIFY:
        if tgt_ext == ".pdf":
            st, msg = _validate(src, ".pdf", tools)
            verdict = _pdfa_verdict(st, msg)
            if st == V.OK:
                return dict(conversion_status="NOT_NEEDED", rename_status="ALREADY_VALID_VERIFIED",
                            verification_status=verdict, action="verify_only")
            code = C.ST_PDFA_NOT_VALIDATED if st == V.UNAVAILABLE else C.ST_PDFA_CONV_REQUIRED
            # 原 PDF 不覆蓋、不移動、不改名
            return dict(conversion_status=code, rename_status="NOT_DONE", verification_status=verdict,
                        error="original PDF untouched; no automatic overwrite", action="verify_only")
        st, msg = _validate(src, tgt_ext, tools)
        return dict(conversion_status="NOT_NEEDED", rename_status="ALREADY_VALID_VERIFIED" if st == V.OK else "NOT_DONE",
                    verification_status=f"{st}: {msg}", action="verify_only")

    # ---- 既有 DP2 檔名只差副檔名大小寫：驗證來源本身是有效 JPEG/TIFF，再 in-place 兩段式改名 ----
    if r.planned_action == A_EXT_NORM:
        st, msg = _validate(src, tgt_ext, tools)
        if st != V.OK:
            return dict(conversion_status="NOT_NEEDED", rename_status="NOT_DONE",
                        verification_status=f"{st}: {msg}", error="source is not a valid image; name not changed")
        _two_step_case_rename(src, tgt)
        return dict(conversion_status="NOT_NEEDED", rename_status="RENAMED_IN_PLACE",
                    verification_status=f"{st}: {msg}", action="normalize_ext", target_sha256=sha256(tgt),
                    target_size=os.path.getsize(tgt))

    # ---- 轉檔／複製到 staging ----
    sdir = os.path.join(staging, r.group_id, r.sequence_number)
    os.makedirs(sdir, exist_ok=True)
    base = os.path.join(sdir, os.path.splitext(r.target_filename)[0])
    src_dur = None
    if kind in ("img_copy", "img_conv"):
        st, staged, msg = CV.convert_image(src, base, kind)
    elif kind in ("doc", "rtf"):
        st, staged, msg = CV.convert_doc(src, sdir, tools)
    elif kind == "txt":
        st, staged, msg = CV.convert_txt(src, sdir, tools)
    elif kind == "html":
        st, staged, msg = CV.convert_html(src, sdir, tools)
    elif kind == "eml":
        st, staged, msg = CV.convert_eml(src, sdir, tools)
        if st != CV.OK and msg.startswith("REVIEW_REQUIRED"):
            return dict(conversion_status=C.ST_REVIEW, rename_status="NOT_DONE", error=msg)
    elif kind == "av":
        staged = base + ".mp4"
        st, msg, src_dur = CV.convert_av(src, staged, tools)
    else:
        return dict(conversion_status="UNSUPPORTED", rename_status="NOT_DONE", error=f"kind={kind}")
    if st != CV.OK:
        return dict(conversion_status="CONVERSION_FAILED", rename_status="NOT_DONE", error=msg)
    final_ext = os.path.splitext(staged)[1].lower()
    if final_ext != tgt_ext:       # 例如 PNG 含 alpha → 轉成 .tif，與 manifest 的 .jpg 不同 → 需人工確認
        return dict(conversion_status="DEVIATES_FROM_MANIFEST", rename_status="NOT_DONE",
                    error=f"produced {final_ext}, manifest says {tgt_ext}; update manifest and re-approve")
    # ---- 驗證輸出 ----
    vst, vmsg = _validate(staged, tgt_ext, tools, src_dur)
    vtxt = _pdfa_verdict(vst, vmsg) if tgt_ext == ".pdf" else f"{vst}: {vmsg}"
    if vst != V.OK:
        if tgt_ext == ".pdf" and vst == V.UNAVAILABLE:
            # 轉檔完成但無法驗證 PDF/A：輸出只留在 staging，不正式命名、不搬移原檔
            return dict(conversion_status=C.ST_PDFA_NOT_VALIDATED, rename_status="NOT_DONE",
                        verification_status=vtxt, action="staged_only",
                        error=f"converted PDF kept in staging only ({staged}); not published, original not moved")
        return dict(conversion_status="CONVERTED", rename_status="NOT_DONE", verification_status=vtxt,
                    error="verification did not pass; not finalized; original not moved")
    # ---- 正式命名：collision 檢查（不分大小寫）→ .part → hash 一致 → rename ----
    folder = os.path.dirname(src)
    if any(n.casefold() == r.target_filename.casefold() for n in os.listdir(folder)):
        return dict(conversion_status="CONVERTED", rename_status="COLLISION", verification_status=vtxt,
                    error="target already exists; never overwrite")
    part = tgt + ".part"
    h_stage = sha256(staged)
    with open(staged, "rb") as fi, open(part, "xb") as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    if sha256(part) != h_stage:
        os.remove(part)           # 本工具剛建立的 .part，非來源
        return dict(conversion_status="CONVERTED", rename_status="NOT_DONE", error="copy hash mismatch; original not moved")
    os.rename(part, tgt)
    out = dict(conversion_status="CONVERTED" if r.conversion_required == "yes" else "NOT_NEEDED",
               rename_status="COPIED", verification_status=f"{vtxt}; {msg}", action="copy_new",
               target_sha256=h_stage, target_size=os.path.getsize(tgt))
    # ---- 原始檔：只在以上全部成功後才『搬移』到 _ORIGINALS（不刪除）----
    if originals == "archive":
        dest = os.path.join(archive_root, os.path.relpath(src, root))
        if os.path.exists(dest):
            out["rename_status"] = "COPIED_ARCHIVE_FAILED"
            out["error"] = "archive destination exists; original left in place"
        else:
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(src, dest)
                out["rename_status"] = "ARCHIVED"
                out["archived_path"] = dest
            except OSError as e:
                out["rename_status"] = "COPIED_ARCHIVE_FAILED"
                out["error"] = f"could not move original to _ORIGINALS: {e}"
    return out


def verify_tree(root: str, out_dir: str) -> int:
    """§25 最終驗證。回傳 0 = 全部通過。"""
    import re
    tools = detect()
    pat = re.compile(r"^DP2_99_04_(\d{6}[a-z]+|00)_\d{2,}$")
    problems: List[str] = []
    seen = {}
    for f in scan_fs(root, check_readable=True):
        stem, ext = f.stem, f.ext
        if not pat.match(stem):
            problems.append(f"A: bad filename {f.path}")
        if ext not in (".jpg", ".jpeg", ".tif", ".tiff", ".pdf", ".mp4"):
            problems.append(f"B: bad extension {f.path}")
            continue
        key = f.path.casefold()
        if key in seen:
            problems.append(f"G: duplicate {f.path}")
        seen[key] = 1
        st, msg = _validate(f.path, ".jpg" if ext in (".jpg", ".jpeg") else ".tif" if ext in (".tif", ".tiff") else ext, tools)
        if st != V.OK:
            problems.append(f"C/D/E: {st} {msg} {f.path}")
    with open(os.path.join(out_dir, "final_verification.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(problems) or "ALL CHECKS PASSED")
        fh.write(f"\nfiles checked: {len(seen)}\n")
    return 0 if not problems else 3
