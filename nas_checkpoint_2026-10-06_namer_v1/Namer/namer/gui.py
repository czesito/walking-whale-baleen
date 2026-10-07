"""tkinter 介面（NAMING_SPEC §13 UI-1～UI-9、UI-G）。"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from . import VERSION, records
from .execute import CONFIRM_PHRASE, Aborted, execute, rollback, rollback_preview
from .plan import KEEP, RENAME, REVIEW, SKIP, plan
from .rules import Override, key_of, parts_of, suggest_rules
from .scan import scan_fs

ACTION_LABEL = {RENAME: "改名", KEEP: "不變", REVIEW: "需檢查", SKIP: "略過"}
FILTERS = ["全部", "改名", "需檢查", "不變", "略過"]
FILTER_ACTION = {"改名": RENAME, "需檢查": REVIEW, "不變": KEEP, "略過": SKIP}


def open_path(p):
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:  # noqa
        messagebox.showerror("無法開啟", str(e))


class App:
    def __init__(self, tkroot: tk.Tk):
        self.tk = tkroot
        self.tk.title(f"Namer 命名工具 {VERSION}")
        self.tk.geometry("1440x840")
        self.tk.minsize(1000, 600)
        self._fonts()
        self.q = queue.Queue()
        self.entries = None
        self.plan = None
        self.rules = None
        self.scanned_root = ""
        self.rules_root = None
        self.busy = False
        self.unit_filter = None
        self._build()
        self.tk.after(100, self._poll)

    # ---------- 外觀 ----------
    def _fonts(self):
        fams = set(tkfont.families())
        for fam in ("Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC"):
            if fam in fams:
                for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                    tkfont.nametofont(name).configure(family=fam, size=10)
                break
        st = ttk.Style()
        if "vista" in st.theme_names():
            st.theme_use("vista")
        st.configure("Treeview", rowheight=24)

    def _build(self):
        top = ttk.Frame(self.tk, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="根資料夾").grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.path_var)
        e.grid(row=0, column=1, columnspan=6, sticky="ew", padx=6)
        e.bind("<Return>", lambda _e: self.scan())
        ttk.Button(top, text="瀏覽…", command=self.browse).grid(row=0, column=7, padx=2)
        self.scan_btn = ttk.Button(top, text="掃描預覽", command=self.scan)
        self.scan_btn.grid(row=0, column=8, padx=2)

        ttk.Label(top, text="前綴").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.prefix_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.prefix_var, width=18).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(top, text="年份範圍").grid(row=1, column=2, sticky="e", pady=(6, 0))
        self.y1 = tk.StringVar(); self.y2 = tk.StringVar()
        ttk.Entry(top, textvariable=self.y1, width=6).grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Entry(top, textvariable=self.y2, width=6).grid(row=1, column=4, sticky="w", padx=2, pady=(6, 0))
        ttk.Label(top, text="附件資料夾").grid(row=1, column=5, sticky="e", pady=(6, 0))
        self.att_var = tk.StringVar(value="跟著信件 PDF 改名")
        ttk.Combobox(top, textvariable=self.att_var, state="readonly", width=18,
                     values=["跟著信件 PDF 改名", "保持原名"]).grid(row=1, column=6, sticky="w", padx=6, pady=(6, 0))
        self.apply_btn = ttk.Button(top, text="套用設定", command=self.replan)
        self.apply_btn.grid(row=1, column=8, padx=2, pady=(6, 0))
        top.columnconfigure(6, weight=1)

        pw = ttk.PanedWindow(self.tk, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=10)

        left = ttk.Frame(pw)
        bar = ttk.Frame(left); bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="命名單位（資料夾）").pack(side="left")
        ttk.Button(bar, text="資料夾規則…", command=self.edit_rule).pack(side="right")
        ttk.Button(bar, text="顯示全部", command=self.show_all).pack(side="right", padx=4)
        cols = ("group", "n", "ren", "rev", "ov")
        self.ut = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="browse")
        self.ut.heading("#0", text="資料夾"); self.ut.column("#0", width=230)
        for c, t, w in zip(cols, ("群組", "檔數", "改名", "需檢查", "規則"), (85, 45, 45, 55, 110)):
            self.ut.heading(c, text=t); self.ut.column(c, width=w, anchor="center" if c != "ov" else "w", stretch=False)
        self._scrolled(left, self.ut)
        self.ut.bind("<<TreeviewSelect>>", self.on_unit)
        self.ut.bind("<Double-1>", lambda _e: self.edit_rule())
        pw.add(left, weight=3)

        right = ttk.Frame(pw)
        bar2 = ttk.Frame(right); bar2.pack(fill="x", pady=(0, 4))
        ttk.Label(bar2, text="顯示").pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        cb = ttk.Combobox(bar2, textvariable=self.filter_var, values=FILTERS, state="readonly", width=8)
        cb.pack(side="left", padx=4); cb.bind("<<ComboboxSelected>>", lambda _e: self.fill_rows())
        ttk.Label(bar2, text="搜尋").pack(side="left", padx=(10, 0))
        self.q_var = tk.StringVar()
        se = ttk.Entry(bar2, textvariable=self.q_var, width=30); se.pack(side="left", padx=4)
        self.q_var.trace_add("write", lambda *_: self.fill_rows())
        self.scope_lbl = ttk.Label(bar2, text=""); self.scope_lbl.pack(side="left", padx=10)
        cols = ("folder", "name", "new", "act", "why")
        self.rt = ttk.Treeview(right, columns=cols, show="headings")
        for c, t, w in zip(cols, ("資料夾", "目前名稱", "新名稱", "動作", "原因"), (230, 230, 230, 60, 260)):
            self.rt.heading(c, text=t); self.rt.column(c, width=w, anchor="center" if c == "act" else "w")
        self.rt.tag_configure(RENAME, foreground="#1f5f8b")
        self.rt.tag_configure(REVIEW, background="#fbe9d8")
        self.rt.tag_configure(SKIP, foreground="#888888")
        self._scrolled(right, self.rt)
        pw.add(right, weight=5)

        bot = ttk.Frame(self.tk, padding=(10, 6))
        bot.pack(fill="x")
        self.summary = ttk.Label(bot, text="選擇根資料夾後按「掃描預覽」。預覽不會改動任何檔案。")
        self.summary.pack(side="left")
        self.pb = ttk.Progressbar(bot, length=180, mode="determinate")
        self.pb.pack(side="left", padx=10)
        ttk.Button(bot, text="開啟紀錄資料夾", command=lambda: open_path(records.data_dir("runs"))).pack(side="right")
        ttk.Button(bot, text="還原上次執行…", command=self.do_rollback).pack(side="right", padx=4)
        ttk.Button(bot, text="匯出預覽 CSV…", command=self.export).pack(side="right", padx=4)
        self.exec_btn = ttk.Button(bot, text="執行改名…", command=self.do_execute, state="disabled")
        self.exec_btn.pack(side="right", padx=4)

    def _scrolled(self, parent, tree):
        f = ttk.Frame(parent); f.pack(fill="both", expand=True)
        ys = ttk.Scrollbar(f, orient="vertical", command=tree.yview)
        xs = ttk.Scrollbar(f, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(in_=f, row=0, column=0, sticky="nsew"); ys.grid(row=0, column=1, sticky="ns"); xs.grid(row=1, column=0, sticky="ew")
        tree.lift(f)      # tree 比 f 先建立，要拉到 f 上面才看得到
        f.rowconfigure(0, weight=1); f.columnconfigure(0, weight=1)

    # ---------- 背景工作 ----------
    def _bg(self, fn, done, *args):
        self._set_busy(True)

        def work():
            try:
                self.q.put(("done", done, fn(*args), None))
            except Exception as e:  # noqa
                self.q.put(("done", done, None, e))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, i, n, text = msg
                    self.pb.configure(maximum=max(n, 1), value=i)
                    self.summary.configure(text=text)
                elif msg[0] == "done":
                    _, cb, result, err = msg
                    self._set_busy(False)
                    cb(result, err)
        except queue.Empty:
            pass
        self.tk.after(100, self._poll)

    def _set_busy(self, b):
        self.busy = b
        st = "disabled" if b else "normal"
        self.scan_btn.configure(state=st); self.apply_btn.configure(state=st)
        self.exec_btn.configure(state="disabled" if b or not self._can_exec() else "normal")
        self.pb.configure(mode="indeterminate" if b else "determinate")
        if b:
            self.pb.start(12)
        else:
            self.pb.stop(); self.pb.configure(value=0)

    def _can_exec(self):
        return bool(self.plan and self.plan.renames())

    # ---------- UI-1 / UI-2 ----------
    def browse(self):
        p = filedialog.askdirectory(title="選擇要命名的根資料夾", mustexist=True)
        if p:
            self.path_var.set(os.path.normpath(p))
            self._load_rules(os.path.normpath(p))

    def _load_rules(self, root):
        self.rules_root = root
        self.rules = records.load_rules_for(root)
        self.prefix_var.set(self.rules.prefix)
        self.y1.set(str(self.rules.year_from)); self.y2.set(str(self.rules.year_to))
        self.att_var.set("保持原名" if self.rules.attachments == "keep" else "跟著信件 PDF 改名")

    def _rules_from_form(self):
        r = self.rules or suggest_rules(os.path.basename(self.scanned_root or self.path_var.get()))
        r.prefix = self.prefix_var.get().strip()
        try:
            r.year_from, r.year_to = int(self.y1.get()), int(self.y2.get())
        except ValueError:
            raise ValueError("年份範圍要填數字")
        r.attachments = "keep" if self.att_var.get() == "保持原名" else "follow"
        errs = r.validate()
        if errs:
            raise ValueError("\n".join(errs))
        return r

    def scan(self):
        root = os.path.normpath(self.path_var.get().strip().strip('"'))
        if not root or not os.path.isdir(root):
            messagebox.showerror("找不到資料夾", f"資料夾不存在或無法讀取：\n{root}")
            return
        if root != self.rules_root:
            self._load_rules(root)
        self.path_var.set(root)

        def prog(n):
            self.q.put(("progress", 0, 0, f"掃描中… 已讀 {n} 個檔案"))
        self._bg(lambda: scan_fs(root, prog), lambda res, err: self._scanned(root, res, err))

    def _scanned(self, root, entries, err):
        if err:
            messagebox.showerror("掃描失敗", str(err)); return
        self.entries, self.scanned_root = entries, root
        self.unit_filter = None
        self.replan()

    def replan(self):
        if not self.entries:
            self.scan(); return
        try:
            self.rules = self._rules_from_form()
            self.plan = plan(self.entries, self.rules, self.scanned_root)
        except ValueError as e:
            messagebox.showerror("規則有誤", str(e)); return
        records.save_rules_for(self.scanned_root, self.rules)
        self.fill_units(); self.fill_rows(); self.update_summary()

    # ---------- UI-3 / UI-5 ----------
    def fill_units(self):
        sel = self.unit_filter
        self.ut.delete(*self.ut.get_children())
        for u in self.plan.units:
            iid = key_of(u.folder)
            group = u.group or ("需檢查" if u.reason else "—")
            self.ut.insert("", "end", iid=iid, text=iid,
                           values=(group, sum(u.counts.values()), u.counts.get(RENAME, 0),
                                   u.counts.get(REVIEW, 0), u.override))
        if sel and self.ut.exists(key_of(sel)):
            self.ut.selection_set(key_of(sel)); self.ut.see(key_of(sel))

    def fill_rows(self):
        if not self.plan:
            return
        self.rt.delete(*self.rt.get_children())
        act = FILTER_ACTION.get(self.filter_var.get())
        q = self.q_var.get().strip().casefold()
        shown = 0
        for i, r in enumerate(self.plan.rows):
            if act and r.action != act:
                continue
            if self.unit_filter is not None and r.folder[:len(self.unit_filter)] != self.unit_filter:
                continue
            fk = r.folder_key
            if q and q not in fk.casefold() and q not in r.name.casefold() and q not in r.new_name.casefold():
                continue
            new = r.new_name if r.action in (RENAME, KEEP) else (f"（建議）{r.new_name}" if r.new_name else "")
            name = r.name + ("\\" if r.is_dir else "")
            self.rt.insert("", "end", iid=str(i), values=(fk, name, new, ACTION_LABEL[r.action], r.reason),
                           tags=(r.action,))
            shown += 1
        scope = f"資料夾：{key_of(self.unit_filter)}　" if self.unit_filter is not None else ""
        self.scope_lbl.configure(text=f"{scope}顯示 {shown} 列")

    def update_summary(self):
        c = self.plan.counts()
        txt = (f"改名 {c.get(RENAME, 0)}　不變 {c.get(KEEP, 0)}　需檢查 {c.get(REVIEW, 0)}　略過 {c.get(SKIP, 0)}"
               f"　｜　單位 {len([u for u in self.plan.units if sum(u.counts.values())])}")
        if c.get(REVIEW):
            txt += "　｜　「需檢查」的檔案不會被改名"
        self.summary.configure(text=txt)
        self.exec_btn.configure(state="normal" if self._can_exec() and not self.busy else "disabled")

    def on_unit(self, _e=None):
        sel = self.ut.selection()
        if sel:
            self.unit_filter = parts_of(sel[0])
            self.fill_rows()

    def show_all(self):
        self.unit_filter = None
        self.ut.selection_remove(*self.ut.selection())
        self.fill_rows()

    # ---------- UI-4 ----------
    def edit_rule(self):
        if not self.plan:
            return
        sel = self.ut.selection()
        if not sel:
            messagebox.showinfo("資料夾規則", "先在左邊選一個資料夾。"); return
        RuleDialog(self, parts_of(sel[0]))

    def set_override(self, parts, ov: Override):
        k = key_of(parts)
        if ov.is_default():
            self.rules.overrides.pop(k, None)
        else:
            self.rules.overrides[k] = ov
        self.unit_filter = parts
        self.replan()

    # ---------- UI-6 ----------
    def export(self):
        if not self.plan:
            return
        p = filedialog.asksaveasfilename(title="匯出預覽", defaultextension=".csv",
                                         initialfile="rename_preview.csv", filetypes=[("CSV", "*.csv")])
        if p:
            records.write_preview(self.plan, p)
            messagebox.showinfo("已匯出", p)

    # ---------- UI-G / UI-7 ----------
    def do_execute(self):
        if not self._can_exec():
            return
        ExecDialog(self)

    def run_execute(self, confirm):
        pl, root = self.plan, self.scanned_root

        def prog(i, n, name):
            self.q.put(("progress", i, n, f"改名中 {i}/{n}：{name}"))

        def work():
            return execute(root, pl, confirm, progress=prog)
        self._bg(work, self._executed)

    def _executed(self, res, err):
        if isinstance(err, Aborted):
            messagebox.showerror("已中止", str(err)); return
        if err:
            messagebox.showerror("執行失敗", repr(err)); return
        msg = [f"已改名 {res.done} / {res.planned}"]
        if res.error:
            msg.append(f"\n中途停止：{res.error}\n已改的檔案保留，可用「還原上次執行」改回。")
        elif res.verify_ok:
            msg.append("驗證通過：新名稱都存在、大小不變、檔案總數不變，重新規劃沒有待改名項目。")
        else:
            msg.append("驗證有問題：\n" + "\n".join(res.problems[:15]))
        msg.append(f"\n紀錄：{res.run_dir}")
        (messagebox.showinfo if res.verify_ok else messagebox.showwarning)("執行完成", "\n".join(msg))
        self.entries = None
        root = self.scanned_root
        self._bg(lambda: scan_fs(root), lambda r, e: self._scanned(root, r, e))

    # ---------- UI-8 ----------
    def do_rollback(self):
        run = records.latest_run_with_log()
        root = self.scanned_root or self.path_var.get().strip()
        if not run:
            messagebox.showinfo("還原", "沒有執行紀錄。"); return
        if not root or not os.path.isdir(root):
            messagebox.showerror("還原", "請先填入當初執行的根資料夾。"); return
        log = os.path.join(run, "rename_log.csv")
        can, cannot = rollback_preview(root, log)
        if not can:
            messagebox.showinfo("還原", f"紀錄 {os.path.basename(run)} 沒有可還原的項目。\n" + "\n".join(cannot[:10])); return
        ok = askstring_confirm(self.tk, "還原上次執行",
                               f"紀錄：{os.path.basename(run)}\n根資料夾：{root}\n可還原 {len(can)} 筆，略過 {len(cannot)} 筆。\n\n"
                               f"輸入「{CONFIRM_PHRASE}」後按確定：")
        if ok != CONFIRM_PHRASE:
            return
        self._bg(lambda: rollback(root, log, ok), self._rolled)

    def _rolled(self, res, err):
        if err:
            messagebox.showerror("還原失敗", str(err)); return
        n, cannot = res
        messagebox.showinfo("還原完成", f"已還原 {n} 筆。" + ("\n略過：\n" + "\n".join(cannot[:10]) if cannot else ""))
        if self.scanned_root:
            root = self.scanned_root
            self._bg(lambda: scan_fs(root), lambda r, e: self._scanned(root, r, e))


class RuleDialog(tk.Toplevel):
    def __init__(self, app: App, parts: tuple):
        super().__init__(app.tk)
        self.app = app
        self.title("資料夾規則")
        self.transient(app.tk); self.resizable(False, False)
        f = ttk.Frame(self, padding=14); f.pack(fill="both")
        ttk.Label(f, text="套用到").grid(row=0, column=0, sticky="w")
        choices = [key_of(parts[:i]) for i in range(len(parts), 0, -1)]
        self.target = tk.StringVar(value=choices[0])
        cb = ttk.Combobox(f, textvariable=self.target, values=choices, state="readonly", width=60)
        cb.grid(row=0, column=1, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.load())
        ttk.Label(f, text="（可選上層資料夾，例如讓整個「混咬表演」共用一個群組）", foreground="#666").grid(
            row=1, column=1, sticky="w")
        self.exclude = tk.BooleanVar(); self.share = tk.BooleanVar(); self.code = tk.StringVar()
        ttk.Checkbutton(f, text="排除：這個資料夾（含子資料夾）不處理", variable=self.exclude).grid(
            row=2, column=1, sticky="w", pady=(10, 2))
        ttk.Checkbutton(f, text="子資料夾共用一個群組，序號依子資料夾順序連續", variable=self.share).grid(
            row=3, column=1, sticky="w", pady=2)
        row = ttk.Frame(f); row.grid(row=4, column=1, sticky="w", pady=2)
        ttk.Label(row, text="指定群組代碼").pack(side="left")
        ttk.Entry(row, textvariable=self.code, width=14).pack(side="left", padx=6)
        ttk.Label(row, text="例：20022004（不分配字母）；留空 = 依日期自動", foreground="#666").pack(side="left")
        btns = ttk.Frame(f); btns.grid(row=5, column=1, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="清除規則", command=self.clear).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="套用", command=self.ok).pack(side="left", padx=4)
        self.load()
        self.grab_set()

    def load(self):
        o = self.app.rules.overrides.get(self.target.get()) or Override()
        self.exclude.set(o.exclude); self.share.set(o.share); self.code.set(o.code)

    def clear(self):
        self.app.set_override(parts_of(self.target.get()), Override()); self.destroy()

    def ok(self):
        o = Override(code=self.code.get().strip(), share=self.share.get(), exclude=self.exclude.get())
        self.app.set_override(parts_of(self.target.get()), o); self.destroy()


class ExecDialog(tk.Toplevel):
    def __init__(self, app: App):
        super().__init__(app.tk)
        self.app = app
        self.title("執行改名")
        self.transient(app.tk); self.resizable(False, False)
        c = app.plan.counts()
        f = ttk.Frame(self, padding=16); f.pack(fill="both")
        info = (f"根資料夾：{app.scanned_root}\n前綴：{app.rules.prefix}\n\n"
                f"將改名：{c.get(RENAME, 0)} 筆\n不變：{c.get(KEEP, 0)}　需檢查（不改）：{c.get(REVIEW, 0)}　略過：{c.get(SKIP, 0)}\n\n"
                "執行前會重新掃描；資料夾若在預覽後有變動，會中止且不改任何檔案。\n"
                "改名後會自動驗證，紀錄存在 data\\runs，可還原。")
        ttk.Label(f, text=info, justify="left").pack(anchor="w")
        ttk.Label(f, text=f"確認無誤請輸入「{CONFIRM_PHRASE}」：").pack(anchor="w", pady=(12, 2))
        self.v = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.v, width=24); e.pack(anchor="w"); e.focus_set()
        btns = ttk.Frame(f); btns.pack(anchor="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=4)
        self.go = ttk.Button(btns, text="執行", state="disabled", command=self.run)
        self.go.pack(side="left", padx=4)
        self.v.trace_add("write", lambda *_: self.go.configure(
            state="normal" if self.v.get().strip() == CONFIRM_PHRASE else "disabled"))
        self.grab_set()

    def run(self):
        phrase = self.v.get().strip()
        self.destroy()
        self.app.run_execute(phrase)


def askstring_confirm(parent, title, prompt):
    from tkinter import simpledialog
    s = simpledialog.askstring(title, prompt, parent=parent)
    return (s or "").strip()


def run_gui() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
