"""EML 附件偵測（唯讀）。

定義：
  attachment = 不是郵件本文、也不是「本文用 cid: 引用的內嵌圖片」的 MIME 部件
               （Content-Disposition: attachment、有檔名的非內嵌部件、message/rfc822 都算）。
  內嵌圖片（Content-ID 且被 HTML 本文以 cid: 引用）可在轉 PDF 時嵌入畫面，不算附件。

規格 §5.3：附件不獨立轉檔，視為 EML 原始內容（原 EML 會被搬到 _ORIGINALS，不刪除）。
若 attachment_count > 0，附件內容不會出現在 PDF/A 裡 → 標 REVIEW_REQUIRED，由你決定。
"""
import email
import re
from email import policy
from typing import List, Tuple

from . import config as C


def inspect_eml(path: str) -> Tuple[int, List[str], str]:
    """回傳 (附件數, 附件檔名清單, 錯誤訊息)。解析失敗時 count = -1。"""
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        body = msg.get_body(preferencelist=("html",))
        html = body.get_content() if body is not None else ""
        cited = set(m.lower() for m in re.findall(r"cid:([^\"'\s>)]+)", html or "", flags=re.I))
        names: List[str] = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            if part is body:
                continue
            disp = part.get_content_disposition()
            cid = (part.get("Content-ID") or "").strip("<> ").lower()
            if part.get_content_type() == "message/rfc822":
                names.append(part.get_filename() or "(attached message)")
                continue
            if disp == "attachment":
                names.append(part.get_filename() or "(unnamed)")
            elif disp == "inline" and cid and cid in cited:
                continue
            elif part.get_filename() and not (cid and cid in cited):
                names.append(part.get_filename())
        return len(names), names, ""
    except Exception as e:  # noqa
        return -1, [], f"{type(e).__name__}: {e}"


def enrich_eml(rows) -> None:
    """對 live 掃描的 EML rows 填入 attachment_detected / attachment_count。
    有附件、或無法解析 → REVIEW_REQUIRED（不自行刪除、不另存附件）。
    execute 與 dry-run 必須用同一個函式，否則 manifest digest 會不同。"""
    for r in rows:
        if r.source_extension.lower() != ".eml":
            continue
        n, names, err = inspect_eml(r.source_path)
        if n < 0:
            r.attachment_detected, r.attachment_count = "UNKNOWN", ""
            if r.status in (C.ST_PLANNED, C.ST_CONV_PLANNED):
                r.status, r.reason = C.ST_REVIEW, f"EML_UNPARSEABLE:{err[:80]}"
                r.planned_action = "NONE"
                r.archive_path = ""
                if r.target_filename:
                    r.proposed_target_filename = r.target_filename
            continue
        r.attachment_detected = "YES" if n else "NO"
        r.attachment_count = str(n)
        if n and r.status in (C.ST_PLANNED, C.ST_CONV_PLANNED):
            r.status = C.ST_REVIEW
            r.reason = f"EML_ATTACHMENT_NOT_PRESERVED_IN_PDF: {n} attachment(s)"
            r.note = (r.note + "; " if r.note else "") + "attachments: " + " | ".join(names)[:200]
            r.planned_action = "NONE"
            r.archive_path = ""
            if r.target_filename:
                r.proposed_target_filename = r.target_filename
