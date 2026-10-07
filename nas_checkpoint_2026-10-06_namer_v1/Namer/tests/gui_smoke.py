"""GUI smoke test（AC-8）：在 Xvfb 下操作真實的 tkinter 介面。執行：xvfb-run python tests/gui_smoke.py <截圖目錄>"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("NAMER_HOME", tempfile.mkdtemp(prefix="namer_home_"))
import tkinter as tk  # noqa: E402

from namer import gui  # noqa: E402
from namer.execute import CONFIRM_PHRASE  # noqa: E402
from namer.rules import Override  # noqa: E402

shots = sys.argv[1] if len(sys.argv) > 1 else None
ROOT = os.path.join(tempfile.mkdtemp(), "04 打羊秀(2000-2005)")
paths = ["2003 11 01 DJ阿義\\IMG_1.JPG", "2003 11 01 DJ阿義\\IMG_2.jpg", "2003 11 02 謎樣\\0530  謎樣表演簡介.pdf",
         "2003 11 02 謎樣\\0530  謎樣表演簡介_attachments\\22.jpg", "2003 11 02 謎樣\\notes.doc",
         "2004 01 混咬表演\\dj\\a.jpg", "2004 01 混咬表演\\drum\\b.jpg", "2002   -   2004\\image0019.JPG",
         "2002   -   2004\\Thumbs.db", "00\\DP2_99_04_00_01.pdf"]
for p in paths:
    full = os.path.join(ROOT, *p.split("\\"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "wb").write(b"x")

root = tk.Tk()
app = gui.App(root)
infos = []
gui.messagebox.showinfo = lambda t, m, **k: infos.append((t, m))
gui.messagebox.showwarning = lambda t, m, **k: infos.append((t, m))
gui.messagebox.showerror = lambda t, m, **k: infos.append(("ERROR " + t, m))


def pump(sec=0.5):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def snap(name):
    if shots:
        pump(0.3)
        os.system(f'import -window root "{os.path.join(shots, name)}"')


pump()
app.path_var.set(ROOT)
app.scan()
pump(1.5)
assert app.plan is not None, "掃描後沒有 plan"
assert app.prefix_var.get() == "DP2_99_04_", app.prefix_var.get()
c = app.plan.counts()
print("初次預覽", dict(c))
assert c["REVIEW"] == 2  # 2002 - 2004 未指定代碼 + notes.doc
snap("1_preview.png")

app.set_override(("2004 01 混咬表演",), Override(share=True))
app.set_override(("2002   -   2004",), Override(code="20022004"))
pump()
c = app.plan.counts()
print("套用規則後", dict(c))
assert c["REVIEW"] == 1 and c["RENAME"] == 7, c
app.unit_filter = None
app.fill_rows()
snap("2_rules_applied.png")

# 規則對話框可開啟
app.ut.selection_set("2004 01 混咬表演\\dj")
pump()
app.edit_rule()
pump()
dlg = [w for w in root.winfo_children() if isinstance(w, gui.RuleDialog)][0]
assert dlg.target.get() == "2004 01 混咬表演\\dj"
dlg.target.set("2004 01 混咬表演"); dlg.load()
assert dlg.share.get() is True
snap("3_rule_dialog.png")
dlg.destroy()

# UI-G：未輸入確認語句時，執行鈕不可按
app.do_execute()
pump()
ed = [w for w in root.winfo_children() if isinstance(w, gui.ExecDialog)][0]
assert str(ed.go["state"]) == "disabled"
ed.v.set("確認")
pump()
assert str(ed.go["state"]) == "disabled"
ed.v.set(CONFIRM_PHRASE)
pump()
assert str(ed.go["state"]) == "normal"
snap("4_confirm.png")
ed.run()
pump(2.5)
print("訊息", infos[-1])
assert infos and infos[-1][0] == "執行完成" and "驗證通過" in infos[-1][1], infos
assert app.plan.counts().get("RENAME", 0) == 0, app.plan.counts()
assert os.path.exists(os.path.join(ROOT, "2004 01 混咬表演", "drum", "DP2_99_04_200401a_02.jpg"))
assert os.path.exists(os.path.join(ROOT, "2003 11 02 謎樣", "DP2_99_04_200311b_01_attachments", "22.jpg"))
snap("5_after.png")

# 還原
gui.askstring_confirm = lambda *a, **k: CONFIRM_PHRASE
app.do_rollback()
pump(2)
print("還原", infos[-1])
assert infos[-1][0] == "還原完成"
assert os.path.exists(os.path.join(ROOT, "2003 11 01 DJ阿義", "IMG_1.JPG"))
assert app.plan.counts()["RENAME"] == 7
root.destroy()
print("GUI SMOKE OK")
