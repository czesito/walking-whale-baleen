import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from nas_rename.folderparse import parse_folder_name
from nas_rename.planner import plan, seq_str, manifest_digest
from nas_rename.scanner import SourceFile

ROOT = r"\\nas\x\04 打羊秀(2000-2005)"


def F(folder, name, size=10):
    parts = tuple(folder.split("\\")) + (name,) if folder else (name,)
    return SourceFile(ROOT + "\\" + "\\".join(parts), parts, size)


def by_name(rows):
    return {r.source_filename: r for r in rows}


class Parse(unittest.TestCase):
    def test_ok(self):
        for name, ym, d in [("2003 11 01 DJ阿義(豆皮員工簡正義)", "200311", 1), ("2001 09賀香滋 大提琴演奏", "200109", None),
                            ("2004 06 12電音22", "200406", 12), ("2002 03 29-31 Wake Up(威爾剛)", "200203", 29),
                            ("2003 11", "200311", None), ("2001 11 Dizzy Daisy", "200111", None)]:
            fd = parse_folder_name(name)
            self.assertTrue(fd.ok, name); self.assertEqual((fd.yyyymm, fd.day), (ym, d), name)

    def test_bad(self):
        for name in ["00", "2002   -   2004", "dj", "1999 01 01 x", "2003 13 01 x", "2003 02 30 x", "混咬表演"]:
            self.assertFalse(parse_folder_name(name).ok, name)


class Plan(unittest.TestCase):
    def test_letters_and_seq(self):
        files = [F("2003 11 20 展覽", "b.jpg"), F("2003 11 01 DJ阿義", "B.doc"), F("2003 11 01 DJ阿義", "a.JPG"),
                 F("2003 11 15 活動", "x.avi"), F("2003 11 無日期", "z.txt")]
        rows = by_name(plan(files, ROOT))
        self.assertEqual(rows["a.JPG"].target_filename, "DP2_99_04_200311a_01.jpg")
        self.assertEqual(rows["B.doc"].target_filename, "DP2_99_04_200311a_02.pdf")
        self.assertEqual(rows["x.avi"].target_filename, "DP2_99_04_200311b_01.mp4")
        self.assertEqual(rows["b.jpg"].target_filename, "DP2_99_04_200311c_01.jpg")
        self.assertEqual(rows["z.txt"].target_filename, "DP2_99_04_200311d_01.pdf")   # 無日 → 排最後

    def test_order_independent_of_input_order(self):
        a = [F("2003 11 02 b", "1.jpg"), F("2003 11 01 a", "2.jpg"), F("2003 11 01 a", "1.jpg")]
        r1 = {x.source_path: x.target_filename for x in plan(a, ROOT)}
        r2 = {x.source_path: x.target_filename for x in plan(list(reversed(a)), ROOT)}
        self.assertEqual(r1, r2)

    def test_same_stem_ext_tiebreak(self):
        rows = by_name(plan([F("2003 11 01 a", "謎樣.eml"), F("2003 11 01 a", "謎樣.doc")], ROOT))
        self.assertEqual(rows["謎樣.doc"].sequence_number, "01"); self.assertEqual(rows["謎樣.eml"].sequence_number, "02")

    def test_seq_width(self):
        self.assertEqual([seq_str(n) for n in (1, 9, 10, 99, 100)], ["01", "09", "10", "99", "100"])

    def test_unknown_ext_and_empty(self):
        rows = by_name(plan([F("2003 11 01 a", "x.xyz"), F("2003 11 01 a", "e.jpg", 0)], ROOT))
        self.assertEqual(rows["x.xyz"].status, "CONVERSION_UNSUPPORTED"); self.assertEqual(rows["e.jpg"].status, "UNREADABLE")

    def test_digest_stable(self):
        f = [F("2003 11 01 a", "1.jpg")]
        self.assertEqual(manifest_digest(plan(f, ROOT)), manifest_digest(plan(f, ROOT)))

    def test_letter_overflow(self):
        files = [F(f"2003 11 {d:02d} n{d}", "a.jpg") for d in range(1, 28)]
        rows = plan(files, ROOT)
        self.assertEqual(sum(1 for r in rows if r.reason.startswith("LETTER_OVERFLOW")), 1)

    def test_rtf_is_document(self):
        r = plan([F("2003 11 01 a", "x.rtf")], ROOT)[0]
        self.assertEqual((r.status, r.target_extension), ("CONVERSION_PLANNED", ".pdf"))
        self.assertIn("LibreOffice", r.conversion_method)


class ExistingNamesWin(unittest.TestCase):
    """決策 1：既有 DP2 命名優先；不重新分配、不改編號。"""

    def test_200303_existing_a_is_kept_and_new_gets_next_letter(self):
        files = [F("2003 03 30 蘇瑋婷", "t1.wma"), F("2003 03 30 蘇瑋婷", "t2.wma"),
                 F("2003 03 黃籃白", "DP2_99_04_200303a_01.jpg"), F("2003 03 黃籃白", "DP2_99_04_200303a_02.JPG")]
        rows = plan(files, ROOT)
        rn = by_name(rows)
        self.assertEqual(rn["DP2_99_04_200303a_01.jpg"].status, "ALREADY_VALID")
        self.assertEqual(rn["DP2_99_04_200303a_02.JPG"].target_filename, "DP2_99_04_200303a_02.jpg")
        self.assertEqual(rn["t1.wma"].target_filename, "DP2_99_04_200303b_01.mp4")
        self.assertEqual(rn["t2.wma"].target_filename, "DP2_99_04_200303b_02.mp4")
        # 既有檔案的 letter/## 永遠不變
        for r in rows:
            if r.name_origin == "EXISTING_DP2":
                self.assertEqual(os.path.splitext(r.source_filename)[0], os.path.splitext(r.target_filename)[0])

    def test_existing_letters_not_shifted_by_earlier_dated_new_folder(self):
        files = [F("2003 03 01 新資料夾", "n.jpg"), F("2003 03 黃籃白", "DP2_99_04_200303a_01.jpg"),
                 F("2003 03 15 另一個", "DP2_99_04_200303b_01.jpg")]
        rn = by_name(plan(files, ROOT))
        self.assertEqual(rn["DP2_99_04_200303a_01.jpg"].folder_letter, "a")
        self.assertEqual(rn["DP2_99_04_200303b_01.jpg"].folder_letter, "b")
        self.assertEqual(rn["n.jpg"].folder_letter, "c")          # 只能用尚未被占用的字母

    def test_new_letters_fill_by_date_order_skipping_occupied(self):
        files = [F("2003 03 02 B", "b.jpg"), F("2003 03 01 A", "a.jpg"), F("2003 03 X", "DP2_99_04_200303a_01.jpg")]
        rn = by_name(plan(files, ROOT))
        self.assertEqual(rn["a.jpg"].folder_letter, "b"); self.assertEqual(rn["b.jpg"].folder_letter, "c")

    def test_mixed_folder_keeps_existing_numbers_new_gets_next_free(self):
        files = [F("2002 05 18 x", f"DP2_99_04_200205b_0{i}.jpg") for i in range(1, 8)] + [F("2002 05 18 x", "五月.doc")]
        rows = by_name(plan(files, ROOT))
        self.assertEqual(rows["五月.doc"].target_filename, "DP2_99_04_200205b_08.pdf")
        self.assertEqual(rows["DP2_99_04_200205b_03.jpg"].sequence_number, "03")

    def test_existing_name_with_other_extension_keeps_stem(self):
        r = plan([F("2002 02 28 x", "DP2_99_04_200202b_02.bmp")], ROOT)[0]
        self.assertEqual((r.status, r.target_filename), ("CONVERSION_PLANNED", "DP2_99_04_200202b_02.jpg"))
        self.assertEqual(r.planned_action, "CONVERT_THEN_ARCHIVE")

    def test_existing_ext_case_only_is_normalization(self):
        r = plan([F("2003 11 01 a", "DP2_99_04_200311a_02.JPG")], ROOT)[0]
        self.assertEqual((r.status, r.planned_action), ("PLANNED", "NORMALIZE_EXT_IN_PLACE"))
        self.assertEqual(r.target_filename, "DP2_99_04_200311a_02.jpg")
        self.assertEqual(r.archive_path, "")          # 純改名，沒有原檔要搬移

    def test_unnamed_jpg_gets_official_name_and_will_be_archived(self):
        r = plan([F("2003 11 01 a", "IMG.JPG")], ROOT)[0]
        self.assertEqual(r.target_filename, "DP2_99_04_200311a_01.jpg")
        self.assertEqual(r.planned_action, "COPY_THEN_ARCHIVE")
        self.assertEqual(r.archive_path, ROOT + "\\_ORIGINALS\\2003 11 01 a\\IMG.JPG")
        self.assertEqual(r.source_relative_path, "2003 11 01 a\\IMG.JPG")

    def test_existing_letter_shared_by_two_folders_is_review(self):
        files = [F("2003 03 A", "DP2_99_04_200303a_01.jpg"), F("2003 03 B", "DP2_99_04_200303a_01.jpg")]
        self.assertTrue(all(r.status == "REVIEW_REQUIRED" for r in plan(files, ROOT)))

    def test_collision_target_occupied(self):
        files = [F("2003 11 01 a", "a.jpg"), F("2003 11 01 a", "b.jpg"), F("2003 11 01 a", "DP2_99_04_200311a_02.jpg")]
        rows = by_name(plan(files, ROOT))
        # 既有 _02 保留；新檔 a.jpg → _01，b.jpg → _03；沒有碰撞
        self.assertEqual(rows["DP2_99_04_200311a_02.jpg"].status, "ALREADY_VALID")
        self.assertEqual(rows["a.jpg"].target_filename, "DP2_99_04_200311a_01.jpg")
        self.assertEqual(rows["b.jpg"].target_filename, "DP2_99_04_200311a_03.jpg")

    def test_00_series_kept_not_renamed(self):
        rows = plan([F("00", "DP2_99_04_00_01.pdf"), F("00", "DP2_99_04_00_02.pdf")], ROOT)
        for r in rows:
            self.assertEqual((r.status, r.planned_action, r.target_filename), ("ALREADY_VALID", "VERIFY_ONLY", r.source_filename))
            self.assertEqual((r.parsed_yyyymm, r.folder_letter), ("", ""))


class Nested(unittest.TestCase):
    """決策 2：巢狀子資料夾沿用最近有日期的祖先，各占一個字母。"""

    def setUp(self):
        self.files = ([F("2004 01 10 甲", "a.jpg"), F("2004 01 混咬表演\\dj", "d.jpg"), F("2004 01 混咬表演\\drum", "e.jpg"),
                       F("2004 01 31 古樂器之夜\\三弦", "s.jpg"), F("2004 01 25 乙", "DP2_99_04_200401a_01.jpg")])

    def test_inherit_and_independent_letters(self):
        rn = by_name(plan(self.files, ROOT))
        self.assertEqual(rn["DP2_99_04_200401a_01.jpg"].folder_letter, "a")       # 既有不動
        for n in ("a.jpg", "d.jpg", "e.jpg", "s.jpg"):
            self.assertEqual(rn[n].parsed_yyyymm, "200401")
            self.assertNotEqual(rn[n].status, "REVIEW_REQUIRED")
        self.assertEqual(len({rn[n].folder_letter for n in ("a.jpg", "d.jpg", "e.jpg", "s.jpg")}), 4)   # 各占一個

    def test_date_source_folder_recorded(self):
        rn = by_name(plan(self.files, ROOT))
        self.assertEqual(rn["d.jpg"].date_source_folder, ROOT + "\\2004 01 混咬表演")
        self.assertEqual(rn["s.jpg"].date_source_folder, ROOT + "\\2004 01 31 古樂器之夜")
        self.assertEqual(rn["a.jpg"].date_source_folder, ROOT + "\\2004 01 10 甲")      # 自己解析 → 自己

    def test_adding_nested_does_not_change_other_letters(self):
        base = [f for f in self.files if "混咬" not in f.path and "古樂器" not in f.path]
        before = {r.source_path: r.target_filename for r in plan(base, ROOT)}
        after = {r.source_path: r.target_filename for r in plan(self.files, ROOT)}
        for k, v in before.items():
            self.assertEqual(after[k], v)

    def test_nested_without_any_dated_ancestor_is_review(self):
        r = plan([F("雜項\\dj", "x.jpg")], ROOT)[0]
        self.assertEqual(r.status, "REVIEW_REQUIRED")


class Review(unittest.TestCase):
    def test_2002_2004_is_review_and_untouched(self):
        rows = plan([F("2002   -   2004", f"image{n:04d}.JPG") for n in range(19, 22)], ROOT)
        for r in rows:
            self.assertEqual(r.status, "REVIEW_REQUIRED")
            self.assertEqual((r.target_filename, r.parsed_yyyymm, r.archive_path), ("", "", ""))
            self.assertEqual(r.planned_action, "NONE")

    def test_loose_file_under_root_is_review(self):
        self.assertEqual(plan([F("", "loose.jpg")], ROOT)[0].status, "REVIEW_REQUIRED")


class EmlAttachments(unittest.TestCase):
    def _eml(self, with_att):
        import tempfile
        from email.message import EmailMessage
        m = EmailMessage(); m["From"] = "a@x"; m["To"] = "b@x"; m["Subject"] = "s"; m["Date"] = "Mon, 1 Jan 2004 10:00:00 +0800"
        m.set_content("hello")
        if with_att:
            m.add_attachment(b"abc", maintype="application", subtype="octet-stream", filename="f.bin")
        d = tempfile.mkdtemp(); p = os.path.join(d, "m.eml"); open(p, "wb").write(bytes(m)); return p

    def test_detect_and_flag(self):
        from nas_rename.eml_info import inspect_eml, enrich_eml
        from nas_rename.planner import Row
        for with_att in (False, True):
            p = self._eml(with_att)
            n, names, err = inspect_eml(p)
            self.assertEqual(n, 1 if with_att else 0, err)
            r = Row(p, "", "m.eml", ".eml", status="CONVERSION_PLANNED", target_filename="DP2_99_04_200401a_01.pdf",
                    planned_action="CONVERT_THEN_ARCHIVE", archive_path="x")
            enrich_eml([r])
            self.assertEqual(r.attachment_detected, "YES" if with_att else "NO")
            self.assertEqual(r.attachment_count, "1" if with_att else "0")
            self.assertEqual(r.status, "REVIEW_REQUIRED" if with_att else "CONVERSION_PLANNED")
            if with_att:
                self.assertEqual((r.planned_action, r.archive_path), ("NONE", ""))


if __name__ == "__main__":
    unittest.main()
