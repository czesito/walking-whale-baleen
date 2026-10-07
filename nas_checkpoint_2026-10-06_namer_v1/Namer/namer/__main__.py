"""CLI（NAMING_SPEC §13）。不帶參數 → 開啟 GUI。

  python -m namer                                  開啟 GUI
  python -m namer plan  <root> [--rules R.json] [--listing L.csv] [--out preview.csv]
  python -m namer apply <root> --rules R.json --confirm "確認 rename"
  python -m namer verify <root> --rules R.json --log rename_log.csv
  python -m namer rollback <root> --log rename_log.csv --confirm "確認 rename"
"""
import argparse
import os
import sys

from . import records
from .execute import Aborted, execute, rollback, rollback_preview, verify
from .plan import plan
from .rules import Rules
from .scan import scan_fs, scan_listing


def _rules(root, path):
    if path:
        return Rules.from_json(open(path, encoding="utf-8").read())
    return records.load_rules_for(root)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .gui import run_gui
        return run_gui()
    ap = argparse.ArgumentParser(prog="namer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("root"); p.add_argument("--rules"); p.add_argument("--listing")
    p.add_argument("--out")
    a = sub.add_parser("apply"); a.add_argument("root"); a.add_argument("--rules"); a.add_argument("--confirm", default="")
    v = sub.add_parser("verify"); v.add_argument("root"); v.add_argument("--rules"); v.add_argument("--log", required=True)
    r = sub.add_parser("rollback"); r.add_argument("root"); r.add_argument("--log", required=True)
    r.add_argument("--confirm", default="")
    args = ap.parse_args(argv)
    rules = _rules(args.root, getattr(args, "rules", None))

    if args.cmd == "plan":
        entries = scan_listing(args.listing, args.root) if args.listing else scan_fs(args.root)
        pl = plan(entries, rules, args.root)
        c = pl.counts()
        print(f"檔案/資料夾 {len(pl.rows)} 列：RENAME {c['RENAME']}  KEEP {c['KEEP']}  REVIEW {c['REVIEW']}  SKIP {c['SKIP']}")
        if args.out:
            records.write_preview(pl, args.out)
            print(f"預覽：{args.out}")
        return 0
    if args.cmd == "apply":
        pl = plan(scan_fs(args.root), rules, args.root)
        print(f"將改名 {len(pl.renames())} 筆")
        try:
            res = execute(args.root, pl, args.confirm)
        except Aborted as e:
            print(f"中止：{e}")
            return 3
        print(f"已改名 {res.done}/{res.planned}；驗證：{'通過' if res.verify_ok else res.problems or res.error}")
        print(f"紀錄：{res.run_dir}")
        return 0 if res.verify_ok else 2
    if args.cmd == "verify":
        probs, _ = verify(args.root, rules, args.log)
        print("驗證通過" if not probs else "\n".join(probs))
        return 0 if not probs else 2
    if args.cmd == "rollback":
        if args.confirm:
            try:
                n, cannot = rollback(args.root, args.log, args.confirm)
            except Aborted as e:
                print(f"中止：{e}")
                return 3
            print(f"已還原 {n} 筆"); print("\n".join(cannot))
        else:
            can, cannot = rollback_preview(args.root, args.log)
            print(f"可還原 {len(can)} 筆（加 --confirm \"確認 rename\" 才執行）"); print("\n".join(cannot))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
