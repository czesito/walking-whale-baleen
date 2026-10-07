"""驗收測試：對照 NAMING_SPEC 的 N-*、S-*、AC-*。執行：python -m unittest discover -s tests"""
import csv
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("NAMER_HOME", tempfile.mkdtemp(prefix="namer_home_"))

from namer.execute import Aborted, CONFIRM_PHRASE, execute, rollback, rollback_preview, verify  # noqa: E402
from namer.folderdate import parse_folder_name  # noqa: E402
from namer.plan import plan  # noqa: E402
from namer.rules import Override, Rules, key_of, suggest_rules  # noqa: E402
from namer.scan import Entry, scan_fs, scan_listing  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")
P = "DP2_99_04_"


def R(**kw):
    r = Rules(prefix=P, year_from=2000, year_to=2005)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def F(path, size=10):
    parts = tuple(path.split("\\"))
    return Entry(parts, False, size)


def D(path):
    return Entry(tuple(path.split("\\")), True)


def by(pl):
    return {key_of(r.folder + (r.name,)): r for r in pl.rows}


def make_tree(root, paths, size=10):
    for p in paths:
        full = os.path.join(root, *p.split("\\"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(b"x" * size)


# ---------------------------------------------------------------- N-DATE
class DateParse(unittest.TestCase):
    def test_ok(self):
        for name, ym, d in [("2003 11 01 DJ阿義(豆皮員工簡正義)", "200311", 1), ("2001 09賀香滋 大提琴演奏", "200109", None),
                            ("2004 06 12電音22", "200406", 12), ("2002 03 29-31 Wake Up(威爾剛)", "200203", 29),
                            ("2003 11", "200311", None), ("20031101 x", "200311", 1), ("2003-11-02 y", "200311", 2)]:
            fd = parse_folder_name(name, 2000, 2005)
            self.assertTrue(fd.ok, name)
            self.assertEqual((fd.yyyymm, fd.day), (ym, d), name)

    def test_bad(self):
        for name in ["00", "2002   -   2004", "dj", "1999 01 01 x", "2003 13 01 x", "2003 02 30 x", "混咬表演", "2004-2005 x"]:
            self.assertFalse(parse_folder_name(name, 2000, 2005).ok, name)

    def test_suggest_rules(self):
        r = suggest_rules("04 打羊秀(2000-2005)")
        self.assertEqual((r.prefix, r.year_from, r.year_to), ("DP2_99_04_", 2000, 2005))
        r = suggest_rules("雜項")
        self.assertEqual(r.prefix, "DP2_99_00_")


# ---------------------------------------------------------------- AC-1
class GoldenV02(unittest.TestCase):
    """AC-1：10/05 清單、無覆寫 → 每個資料夾的 group 與 RENAME_SPEC v0.2 manifest 相同。"""

    def test_groups_match_v02_manifest(self):
        root = r"\\Aaa_nas\iast\18_豆皮藝文咖啡廳第二期\數位檔案_整理\04 打羊秀(2000-2005)"
        pl = plan(scan_listing(os.path.join(DATA, "listing_20261005.csv"), root),
                  suggest_rules("04 打羊秀(2000-2005)"), root)
        mine = {key_of(u.folder): u.group for u in pl.units}
        exp = {r["folder"]: r["group"] for r in csv.DictReader(open(os.path.join(DATA, "expected_groups_v02.csv"), encoding="utf-8-sig"))}
        self.assertEqual(len(exp), 147)
        self.assertEqual({k: mine.get(k) for k in exp}, exp)
        self.assertEqual(mine["00"], "00")
        self.assertEqual(mine["2002   -   2004"], "")        # 無覆寫 → REVIEW


# ---------------------------------------------------------------- N-EXIST / N-LETTER / N-SEQ
class Naming(unittest.TestCase):
    def test_letters_and_seq(self):
        pl = plan([F("2003 11 20 展覽\\b.jpg"), F("2003 11 01 DJ\\B.pdf"), F("2003 11 01 DJ\\a.JPG"),
                   F("2003 11 15 活動\\x.mp4"), F("2003 11 無日期\\z.pdf")], R())
        b = by(pl)
        self.assertEqual(b["2003 11 01 DJ\\a.JPG"].new_name, P + "200311a_01.jpg")
        self.assertEqual(b["2003 11 01 DJ\\B.pdf"].new_name, P + "200311a_02.pdf")
        self.assertEqual(b["2003 11 15 活動\\x.mp4"].new_name, P + "200311b_01.mp4")
        self.assertEqual(b["2003 11 20 展覽\\b.jpg"].new_name, P + "200311c_01.jpg")
        self.assertEqual(b["2003 11 無日期\\z.pdf"].new_name, P + "200311d_01.pdf")

    def test_200303_existing_kept_new_gets_next(self):
        pl = plan([F("2003 03 30 蘇瑋婷\\t1.mp4"), F("2003 03 30 蘇瑋婷\\t2.mp4"),
                   F(f"2003 03 黃籃白\\{P}200303a_01.jpg"), F(f"2003 03 黃籃白\\{P}200303a_02.JPG")], R())
        b = by(pl)
        self.assertEqual(b[f"2003 03 黃籃白\\{P}200303a_01.jpg"].action, "KEEP")
        self.assertEqual(b[f"2003 03 黃籃白\\{P}200303a_02.JPG"].new_name, P + "200303a_02.jpg")
        self.assertEqual(b["2003 03 30 蘇瑋婷\\t1.mp4"].new_name, P + "200303b_01.mp4")

    def test_mixed_folder_new_file_gets_next_free(self):
        files = [F(f"2002 05 18 x\\{P}200205b_0{i}.jpg") for i in (1, 2, 4)] + [F("2002 05 18 x\\五月.pdf")]
        b = by(plan(files, R()))
        self.assertEqual(b["2002 05 18 x\\五月.pdf"].new_name, P + "200205b_03.pdf")   # 最小未使用

    def test_non_archival_is_review_and_takes_no_number(self):        # N-ARCH
        b = by(plan([F("2003 11 01 a\\a.doc"), F("2003 11 01 a\\b.jpg"), F("2003 11 01 a\\c.AVI")], R()))
        self.assertEqual(b["2003 11 01 a\\a.doc"].action, "REVIEW")
        self.assertEqual(b["2003 11 01 a\\b.jpg"].new_name, P + "200311a_01.jpg")
        self.assertEqual(b["2003 11 01 a\\c.AVI"].action, "REVIEW")

    def test_ext_normalised(self):
        b = by(plan([F("2003 11 01 a\\x.JPEG"), F("2003 11 01 a\\y.TIFF")], R()))
        self.assertEqual(b["2003 11 01 a\\x.JPEG"].new_name, P + "200311a_01.jpg")
        self.assertEqual(b["2003 11 01 a\\y.TIFF"].new_name, P + "200311a_02.tif")

    def test_seq_three_digits(self):
        b = by(plan([F(f"2003 11 01 a\\{i:03d}.jpg") for i in range(1, 102)], R()))
        self.assertEqual(b["2003 11 01 a\\100.jpg"].new_name, P + "200311a_100.jpg")

    def test_00_series_kept_and_new_continues(self):              # Q3
        b = by(plan([F(f"00\\{P}00_01.pdf"), F(f"00\\{P}00_02.pdf"), F("00\\new.pdf")], R()))
        self.assertEqual(b[f"00\\{P}00_01.pdf"].action, "KEEP")
        self.assertEqual(b["00\\new.pdf"].new_name, P + "00_03.pdf")
        self.assertEqual(b["00\\new.pdf"].origin, "CONTINUE")

    def test_existing_date_mismatch_is_review(self):
        b = by(plan([F(f"2003 11 01 a\\{P}200312a_01.jpg"), F("2003 11 01 a\\b.jpg")], R()))
        self.assertTrue(all(r.action == "REVIEW" for r in b.values()))

    def test_existing_multiple_codes_is_review(self):
        b = by(plan([F(f"2003 11 01 a\\{P}200311a_01.jpg"), F(f"2003 11 01 a\\{P}200311b_01.jpg")], R()))
        self.assertTrue(all(r.reason.startswith("EXISTING_MULTIPLE_CODES") for r in b.values()))

    def test_letter_overflow(self):
        pl = plan([F(f"2003 11 {d:02d} n{d}\\a.jpg") for d in range(1, 28)], R())
        self.assertEqual(sum(1 for r in pl.rows if r.reason.startswith("LETTER_OVERFLOW")), 1)

    def test_files_under_root_and_system_files(self):
        b = by(plan([F("loose.jpg"), F("2003 11 01 a\\Thumbs.db"), F("2003 11 01 a\\._x.jpg"), F("2003 11 01 a\\x.jpg")], R()))
        self.assertEqual(b["loose.jpg"].reason, "FILE_DIRECTLY_UNDER_ROOT")
        self.assertEqual(b["2003 11 01 a\\Thumbs.db"].action, "SKIP")
        self.assertEqual(b["2003 11 01 a\\._x.jpg"].action, "SKIP")
        self.assertEqual(b["2003 11 01 a\\x.jpg"].new_name, P + "200311a_01.jpg")   # 系統檔不占號

    def test_inherit_date(self):
        b = by(plan([F("2004 01 混咬表演\\dj\\a.jpg"), F("2004 01 混咬表演\\drum\\b.jpg")], R()))
        self.assertEqual(b["2004 01 混咬表演\\dj\\a.jpg"].new_name, P + "200401a_01.jpg")
        self.assertEqual(b["2004 01 混咬表演\\drum\\b.jpg"].new_name, P + "200401b_01.jpg")
        self.assertEqual(b["2004 01 混咬表演\\dj\\a.jpg"].date_source, "2004 01 混咬表演")


# ---------------------------------------------------------------- AC-2 / AC-3
class V2Decisions(unittest.TestCase):
    """AC-2：重現 04 打羊秀 session4 的決策。"""

    def entries(self):
        e = [F(f"2004 01 10 焦慮\\{P}200401a_01.jpg"), F(f"2004 01 18 謎樣\\{P}200401b_01.jpg"),
             F(f"2004 01 18 黃藍白\\{P}200401c_01.jpg"), F(f"2004 01 24 DJ\\{P}200401d_01.jpg"),
             F(f"2004 01 25 王山福\\{P}200401e_01.jpg"), F(f"2004 01 25 顏滋瑩\\{P}200401f_01.jpg"),
             F(f"2004 01 31 古樂器之夜\\三弦\\{P}200401g_01.jpg"), F(f"2004 01 31 古樂器之夜\\琵琶\\{P}200401g_02.jpg")]
        for sub, n in (("dj", 3), ("drum", 2), ("樂團", 2)):
            e += [F(f"2004 01 混咬表演\\{sub}\\IMG_{i}.jpg") for i in range(1, n + 1)]
        e += [F(f"2002   -   2004\\image{i:04d}.jpg") for i in (19, 20, 21)]
        return e

    def rules(self):
        return R(overrides={"2004 01 混咬表演": Override(share=True), "2002   -   2004": Override(code="20022004")})

    def test_decisions(self):
        pl = plan(self.entries(), self.rules())
        b = by(pl)
        # 三弦＋琵琶 既有 200401g 自動合池（N-SIBLING）
        self.assertEqual(b[f"2004 01 31 古樂器之夜\\琵琶\\{P}200401g_02.jpg"].action, "KEEP")
        # 混咬表演共用 200401h，序號依子資料夾順序連續
        got = [b[f"2004 01 混咬表演\\{s}\\IMG_{i}.jpg"].new_name for s, i in
               (("dj", 1), ("dj", 2), ("dj", 3), ("drum", 1), ("drum", 2), ("樂團", 1), ("樂團", 2))]
        self.assertEqual(got, [f"{P}200401h_{n:02d}.jpg" for n in range(1, 8)])
        # 2002 - 2004 指定代碼，無字母
        self.assertEqual(b["2002   -   2004\\image0019.jpg"].new_name, P + "20022004_01.jpg")
        self.assertEqual(b["2002   -   2004\\image0021.jpg"].new_name, P + "20022004_03.jpg")

    def test_new_file_in_shared_existing_pool(self):
        e = self.entries() + [F("2004 01 31 古樂器之夜\\三弦\\new.jpg")]
        b = by(plan(e, self.rules()))
        self.assertEqual(b["2004 01 31 古樂器之夜\\三弦\\new.jpg"].new_name, P + "200401g_03.jpg")

    def test_unrelated_folders_same_code_is_review(self):
        b = by(plan([F(f"2004 01 10 x\\{P}200401a_01.jpg"), F(f"2004 01 11 y\\{P}200401a_02.jpg")], R()))
        self.assertTrue(all(r.reason.startswith("CODE_SHARED_BY_UNRELATED") for r in b.values()))

    def test_deterministic(self):                                      # AC-3
        e = self.entries()
        ref = {key_of(r.folder + (r.name,)): r.new_name for r in plan(e, self.rules()).rows}
        for seed in range(5):
            random.Random(seed).shuffle(e)
            got = {key_of(r.folder + (r.name,)): r.new_name for r in plan(e, self.rules()).rows}
            self.assertEqual(got, ref)

    def test_rules_json_roundtrip(self):
        r = self.rules()
        self.assertEqual(Rules.from_json(r.to_json()), r)


# ---------------------------------------------------------------- N-CHECK / N-ATT
class Checks(unittest.TestCase):
    def test_prefix_match_is_case_sensitive(self):
        # 前綴大小寫不符的檔案不算既有命名，當作新檔分配號碼（純大小寫改名，不衝突）
        b = by(plan([F("2003 11 01 a\\x.jpg"), F("2003 11 01 a\\dp2_99_04_200311A_01.JPG")], R()))
        self.assertEqual(b["2003 11 01 a\\dp2_99_04_200311A_01.JPG"].new_name, P + "200311a_01.jpg")
        self.assertEqual(b["2003 11 01 a\\x.jpg"].new_name, P + "200311a_02.jpg")

    def test_target_occupied_by_subfolder(self):
        b = by(plan([F("2003 11 01 a\\x.jpg"), D("2003 11 01 a"), D(f"2003 11 01 a\\{P}200311a_01.jpg")], R()))
        self.assertTrue(b["2003 11 01 a\\x.jpg"].reason.startswith("TARGET_OCCUPIED"))

    def test_path_too_long(self):
        b = by(plan([F("2003 11 01 a\\x.jpg")], R(), root="C:\\" + "x" * 230))
        self.assertTrue(b["2003 11 01 a\\x.jpg"].reason.startswith("PATH_TOO_LONG"))

    def test_attachments_follow_mail_pdf(self):                        # AC-7
        e = [D("2004 05 08 慕"), D("2004 05 08 慕\\0508  慕的資料_attachments"),
             F("2004 05 08 慕\\0508  慕的資料.pdf"), F("2004 05 08 慕\\0508  慕的資料_attachments\\往下看~new.jpg"),
             F("2004 05 08 慕\\0508  慕的資料_attachments\\Thumbs.db")]
        b = by(plan(e, R()))
        self.assertEqual(b["2004 05 08 慕\\0508  慕的資料.pdf"].new_name, P + "200405a_01.pdf")
        self.assertEqual(b["2004 05 08 慕\\0508  慕的資料_attachments"].new_name, P + "200405a_01_attachments")
        self.assertEqual(b["2004 05 08 慕\\0508  慕的資料_attachments\\往下看~new.jpg"].action, "SKIP")
        b2 = by(plan(e, R(attachments="keep")))
        self.assertEqual(b2["2004 05 08 慕\\0508  慕的資料_attachments"].action, "SKIP")

    def test_attachment_parent_missing(self):
        e = [D("2004 05 08 慕\\x_attachments"), F("2004 05 08 慕\\x_attachments\\a.jpg"), F("2004 05 08 慕\\b.jpg")]
        self.assertEqual(by(plan(e, R()))["2004 05 08 慕\\x_attachments"].reason, "ATTACHMENT_PARENT_NOT_FOUND")

    def test_exclude(self):
        b = by(plan([F("2003 11 01 a\\x.jpg"), F("2003 11 01 a\\sub\\y.jpg")],
                    R(overrides={"2003 11 01 a": Override(exclude=True)})))
        self.assertTrue(all(r.action == "SKIP" for r in b.values()))

    def test_bad_prefix_rejected(self):
        with self.assertRaises(ValueError):
            plan([], Rules(prefix="DP2 99"))


# ---------------------------------------------------------------- AC-4 / AC-5 / AC-6（真實檔案）
class Execute(unittest.TestCase):
    PATHS = ["2003 11 01 DJ\\IMG_1.JPG", "2003 11 01 DJ\\IMG_2.jpg", f"2003 11 01 DJ\\{P}200311a_05.JPG",
             "2003 11 02 跳跳蛋＆朋友\\0530  謎樣表演簡介.pdf", "2003 11 02 跳跳蛋＆朋友\\0530  謎樣表演簡介_attachments\\22.jpg",
             "2003 11 02 跳跳蛋＆朋友\\Thumbs.db", "2003 11 02 跳跳蛋＆朋友\\notes.doc",
             "2004 01 混咬表演\\dj\\a.jpg", "2004 01 混咬表演\\drum\\b.jpg", "2002   -   2004\\image0019.JPG"]

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="namer_root_")
        make_tree(self.root, self.PATHS)
        self.rules = R(overrides={"2004 01 混咬表演": Override(share=True), "2002   -   2004": Override(code="20022004")})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def names(self):
        out = set()
        for dp, dn, fn in os.walk(self.root):
            rel = os.path.relpath(dp, self.root)
            out |= {os.path.normpath(os.path.join(rel, n)) for n in dn + fn}
        return out

    def test_execute_verify_idempotent_rollback(self):               # AC-4、AC-6
        before = self.names()
        pl = plan(scan_fs(self.root), self.rules, self.root)
        c = pl.counts()
        self.assertEqual(c["RENAME"], 8)    # 6 個新檔 + 既有 .JPG→.jpg + 附件資料夾
        with self.assertRaises(Aborted):
            execute(self.root, pl, "yes")                              # S-4
        self.assertEqual(self.names(), before)
        res = execute(self.root, pl, CONFIRM_PHRASE)
        self.assertEqual((res.done, res.error), (8, ""))
        self.assertTrue(res.verify_ok, res.problems)
        self.assertEqual(res.replan_renames, 0)
        after = self.names()
        self.assertIn(os.path.normpath(f"2003 11 01 DJ/{P}200311a_01.jpg"), after)
        self.assertIn(os.path.normpath(f"2003 11 01 DJ/{P}200311a_05.jpg"), after)
        self.assertIn(os.path.normpath(f"2003 11 02 跳跳蛋＆朋友/{P}200311b_01_attachments/22.jpg"), after)
        self.assertIn(os.path.normpath(f"2004 01 混咬表演/drum/{P}200401a_02.jpg"), after)
        self.assertIn(os.path.normpath(f"2002   -   2004/{P}20022004_01.jpg"), after)
        self.assertIn(os.path.normpath("2003 11 02 跳跳蛋＆朋友/notes.doc"), after)    # REVIEW 不動
        for f in ("rules.json", "preview.csv", "rename_log.csv", "name_map.csv", "summary.txt"):
            self.assertTrue(os.path.exists(os.path.join(res.run_dir, f)), f)
        log = os.path.join(res.run_dir, "rename_log.csv")
        self.assertEqual(verify(self.root, self.rules, log)[0], [])
        can, cannot = rollback_preview(self.root, log)
        self.assertEqual((len(can), cannot), (8, []))
        n, cannot = rollback(self.root, log, CONFIRM_PHRASE)
        self.assertEqual(n, 8)
        self.assertEqual(self.names(), before)

    def test_abort_when_folder_changed(self):                          # AC-5
        pl = plan(scan_fs(self.root), self.rules, self.root)
        make_tree(self.root, ["2003 11 01 DJ\\IMG_0.jpg"])             # 新檔排在最前面 → 號碼位移
        before = self.names()
        with self.assertRaises(Aborted):
            execute(self.root, pl, CONFIRM_PHRASE)
        self.assertEqual(self.names(), before)

    def test_abort_when_size_changed(self):
        pl = plan(scan_fs(self.root), self.rules, self.root)
        make_tree(self.root, ["2003 11 01 DJ\\IMG_2.jpg"], size=99)
        with self.assertRaises(Aborted):
            execute(self.root, pl, CONFIRM_PHRASE)

    def test_system_file_change_does_not_abort(self):
        pl = plan(scan_fs(self.root), self.rules, self.root)
        make_tree(self.root, ["2004 01 混咬表演\\dj\\Thumbs.db", "2003 11 01 DJ\\desktop.ini"])
        res = execute(self.root, pl, CONFIRM_PHRASE)
        self.assertEqual(res.done, 8)
        self.assertTrue(res.verify_ok, res.problems)

    def test_baleen_provenance_in_name_map(self):
        os.makedirs(os.path.join(self.root, "_baleen"))
        with open(os.path.join(self.root, "_baleen", "report-20261006-120000.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "source_path", "output_path"])
            w.writerow(["x", "2003 11 01 DJ/IMG_1.JPG", "2003 11 01 DJ/IMG_1.JPG"])
        pl = plan(scan_fs(self.root), self.rules, self.root)
        self.assertFalse(any("_baleen" in r.folder for r in pl.rows))
        res = execute(self.root, pl, CONFIRM_PHRASE)
        rows = list(csv.DictReader(open(os.path.join(res.run_dir, "name_map.csv"), encoding="utf-8-sig")))
        hit = [r for r in rows if r["final_path"].endswith(f"{P}200311a_01.jpg")]
        self.assertEqual(hit[0]["baleen_source_path"], "2003 11 01 DJ/IMG_1.JPG")
        self.assertEqual(hit[0]["previous_name"], "IMG_1.JPG")


if __name__ == "__main__":
    unittest.main()
