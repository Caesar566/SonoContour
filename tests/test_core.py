import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "BreastContour"))
import numpy as np
from scipy import ndimage as ndi
from BreastContourLib.core import (parse_ranges, workbook_notes, discover_cases,
                                   contour_problems, check_arrays, atomic_save)


def rectangle(shape=(40, 60), x=10, y=8, width=25, height=20):
    a = np.zeros(shape, dtype=bool)
    a[y, x:x+width] = a[y+height-1, x:x+width] = True
    a[y:y+height, x] = a[y:y+height, x+width-1] = True
    return a


class CoreTests(unittest.TestCase):
    # All inline case IDs and range examples below are synthetic fixtures.
    def test_range_inheritance_reversal_and_overlap(self):
        ranges, warnings = parse_ranges("2类 10-30/60-70/4A类 35-45/90-55\\", 100)
        self.assertFalse(warnings)
        self.assertEqual([(r.category, r.start, r.end) for r in ranges],
                         [("2类",10,30),("2类",60,70),("4A类",35,45),("4A类",55,90)])
        ranges, warnings = parse_ranges("2类 10-25/4A类 22-35/40-50")
        self.assertEqual([r.category for r in ranges if r.contains(23)], ["2类", "4A类"])

    def test_individual_tumors_and_category_switch_without_slash(self):
        examples = [
            ("2类 10-20/40-50 3类 70-85", [("2类",10,20),("2类",40,50),("3类",70,85)]),
            ("2类  10-15/25-30/40-48/49-60/3类  40-55", [("2类",10,15),("2类",25,30),("2类",40,48),("2类",49,60),("3类",40,55)]),
            ("2类 10-15/35-45/25-30/", [("2类",10,15),("2类",35,45),("2类",25,30)]),
        ]
        for note, expected in examples:
            with self.subTest(note=note):
                ranges, warnings = parse_ranges(note, 500)
                self.assertFalse(warnings)
                self.assertEqual([(r.category,r.start,r.end) for r in ranges],expected)
        ranges, _ = parse_ranges(examples[1][0])
        self.assertEqual([i for i,r in enumerate(ranges,1) if r.contains(44)], [3,5])
        ranges, warnings = parse_ranges("2类 1-2/1-2/3-4 4A类 5-6")
        self.assertFalse(warnings)
        self.assertEqual(len(ranges),4)
        self.assertEqual(ranges[-1].category,"4A类")

    def test_empty_malformed_and_out_of_bounds(self):
        self.assertTrue(parse_ranges("")[1])
        self.assertTrue(parse_ranges("10-20")[1])
        self.assertTrue(parse_ranges("2类 garbage")[1])
        self.assertEqual(len(parse_ranges("2类 1-2/4A类 garbage/5-6")[0]), 1)
        self.assertTrue(parse_ranges("2类 499-500", 500)[1])
        self.assertFalse(parse_ranges("2类 0-499", 500)[1])

    def test_workbook_stage_isolation(self):
        xml = '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
        <row r="1"><c r="A1" t="inlineStr"><is><t>阶段一</t></is></c><c r="E1" t="inlineStr"><is><t>备注</t></is></c></row>
        <row r="2"><c r="C2" t="inlineStr"><is><t>2.25.123</t></is></c><c r="E2" t="inlineStr"><is><t>错误阶段</t></is></c></row>
        <row r="3"><c r="A3" t="inlineStr"><is><t>阶段三：审核</t></is></c></row>
        <row r="4"><c r="C4" t="inlineStr"><is><t>体位编号</t></is></c><c r="E4" t="inlineStr"><is><t>备注</t></is></c></row>
        <row r="5"><c r="C5" t="inlineStr"><is><t>2.25.123</t></is></c><c r="E5" t="inlineStr"><is><t>2类 1-2</t></is></c></row>
        <row r="6"><c r="A6" t="inlineStr"><is><t>阶段四</t></is></c></row>
        <row r="7"><c r="C7" t="inlineStr"><is><t>2.25.123</t></is></c><c r="E7" t="inlineStr"><is><t>另一个阶段</t></is></c></row>
        </sheetData></worksheet>'''
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"test.xlsx"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("xl/worksheets/sheet1.xml", xml)
            self.assertEqual(workbook_notes(p), {"2.25.123": "2类 1-2"})
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("xl/worksheets/sheet1.xml", xml.replace('r="6"', 'r="9"').replace('阶段四', '阶段三').replace('另一个阶段', '备注'))
            # A separate duplicate-record workbook must be rejected, not overwritten.
            duplicate = xml.replace('<row r="6">', '<row r="8"><c r="C8" t="inlineStr"><is><t>2.25.123</t></is></c></row><row r="6">')
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("xl/worksheets/sheet1.xml", duplicate)
            with self.assertRaises(ValueError):
                workbook_notes(p)

    def test_actual_workbook(self):
        root = Path(os.environ.get("BREAST_CONTOUR_DATA", str(Path(__file__).resolve().parents[1])))
        if not (root/"segmentations.xlsx").exists():
            self.skipTest("sample data unavailable")
        cases = discover_cases(root)
        self.assertGreater(len(cases), 0)
        for case in cases:
            self.assertFalse(case.warnings)
            self.assertTrue(case.image.exists())
            self.assertTrue(case.doctor.exists())

    def test_closed_rectangle(self):
        a = rectangle()
        original = a.copy()
        self.assertEqual(contour_problems(a), [])
        np.testing.assert_array_equal(a, original)

    def test_diagonal_closed_diamond(self):
        a = np.zeros((35,35), dtype=bool)
        y,x = np.indices(a.shape)
        a[np.abs(x-17)+np.abs(y-17)==10] = True
        self.assertEqual(contour_problems(a), [])

    def test_open_gap(self):
        a = rectangle()
        a[8,20] = False
        self.assertIn("疑似未闭合", [p[0] for p in contour_problems(a)])

    def test_thick_closed(self):
        a = ndi.binary_dilation(rectangle(), iterations=2)
        self.assertEqual(contour_problems(a), [])

    def test_branch_on_loop(self):
        a = rectangle()
        a[1:8,20] = True
        self.assertIn("存在分支", [p[0] for p in contour_problems(a)])

    def test_multiple_closed_components(self):
        a = rectangle(x=3, width=15) | rectangle(x=35, width=15)
        self.assertEqual(contour_problems(a), [])

    def test_touching_border(self):
        a = rectangle(x=0)
        self.assertIn("接触图像边界", [p[0] for p in contour_problems(a)])

    def test_empty_and_isolated_point(self):
        a = np.zeros((30,30), dtype=bool)
        self.assertEqual(contour_problems(a), [])
        a[10,10] = True
        self.assertIn("疑似未闭合", [p[0] for p in contour_problems(a)])

    def test_blank_frames_separate_from_open(self):
        a = np.zeros((5,40,60), dtype=np.uint8)
        a[1] = rectangle()
        a[2, 8, 10:30] = 1
        ranges, _ = parse_ranges("2类 1-3")
        issues = check_arrays({"lesion": a}, ranges, [2])
        self.assertTrue(any(i.frame==2 and i.kind=="疑似未闭合" and i.locked for i in issues))
        self.assertEqual([i.frame for i in issues if i.kind=="待确认空帧"], [3])

    def test_atomic_success_and_backup_restore(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/"Segment.nrrd"
            target.write_bytes(b"old")
            backup = atomic_save(target, lambda p: p.write_bytes(b"new"), lambda p: self.assertEqual(p.read_bytes(), b"new"))
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(backup.read_bytes(), b"old")
            target.write_bytes(backup.read_bytes())
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse(list(Path(d).glob(".Segment-*")))

    def test_atomic_validation_failure_preserves_old(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/"Segment.nrrd"
            target.write_bytes(b"old")
            def fail(path):
                raise IOError("invalid output")
            with self.assertRaises(IOError):
                atomic_save(target, lambda p: p.write_bytes(b"new"), fail)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["Segment.nrrd"])

    def test_atomic_writer_and_replace_failure(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/"Segment.nrrd"
            target.write_bytes(b"old")
            with self.assertRaises(PermissionError):
                with patch("BreastContourLib.core.os.replace", side_effect=PermissionError("read only")):
                    atomic_save(target, lambda p: p.write_bytes(b"new"), lambda p: None)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(len(list(Path(d).glob("Segment.backup-*"))), 1)
            with self.assertRaises(IOError):
                atomic_save(target, lambda p: None, lambda p: None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
