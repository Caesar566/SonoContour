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
                                   contour_problems, check_arrays, atomic_save, Case)


def rectangle(shape=(40, 60), x=10, y=8, width=25, height=20):
    a = np.zeros(shape, dtype=bool)
    a[y, x:x+width] = a[y+height-1, x:x+width] = True
    a[y:y+height, x] = a[y:y+height, x+width-1] = True
    return a


class CoreTests(unittest.TestCase):
    @staticmethod
    def write_lesion_workbook(root, rows, suffix=""):
        from xml.sax.saxutils import escape
        xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        for number, values in enumerate(rows, 1):
            xml += f'<row r="{number}">'
            for col, value in values.items():
                if isinstance(value, int):
                    xml += f'<c r="{col}{number}"><v>{value}</v></c>'
                else:
                    xml += f'<c r="{col}{number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            xml += '</row>'
        xml += suffix + '</sheetData></worksheet>'
        with zipfile.ZipFile(root/'segmentations.xlsx','w') as output:
            output.writestr('xl/worksheets/sheet1.xml',xml)

    def test_lesion_layout_inline_ranges_numeric_ids_and_exemption(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            rows=[{'A':'阶段一：病灶标注'},
                  {'C':'体位编号','D':'birads分类','E':'病灶对应帧'},
                  {'C':123456789012345,'D':'2类 10-20'},
                  {'C':'123456789012346','D':'1类'},
                  {'C':'123456789012347','D':'4A类 50-40-30,60-70'}]
            self.write_lesion_workbook(root,rows)
            before=(root/'segmentations.xlsx').read_bytes()
            directory=root/str(rows[2]['C']);directory.mkdir()
            (directory/(directory.name+'_ORI.dcm')).touch()
            (directory/'Segmentation.nrrd').touch()
            cases={c.uid:c for c in discover_cases(root)}
            case=cases[str(rows[2]['C'])]
            self.assertEqual(case.note,'2类 10-20')
            self.assertEqual([(r.category,r.start,r.end) for r in case.ranges],[('2类',10,20)])
            self.assertFalse(case.warnings)
            self.assertTrue(cases[rows[3]['C']].annotation_exempt)
            self.assertEqual(cases[rows[4]['C']].ranges[0].middle,40)
            self.assertEqual(len(cases[rows[4]['C']].ranges),2)
            self.assertEqual((root/'segmentations.xlsx').read_bytes(),before)
            self.assertFalse(case.output.exists())

    def test_lesion_layout_separate_frames_and_explicit_category(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.write_lesion_workbook(root,[
                {'B':'体位编号','F':'BI-RADS 分类','H':'病灶对应帧'},
                {'B':'2.25.123','F':'4A类','H':'10-15-20,30-40'},
                {'B':'2.25.124','F':'2类','H':'3类 10-20'},
                {'B':'2.25.125','F':'2类'}])
            cases={c.uid:c for c in discover_cases(root)}
            self.assertEqual([r.category for r in cases['2.25.123'].ranges],['4A类','4A类'])
            self.assertEqual(cases['2.25.124'].ranges[0].category,'3类')
            self.assertFalse(cases['2.25.125'].annotation_exempt)
            self.assertTrue(cases['2.25.125'].warnings)

    def test_lesion_layout_stage_three_precedence_and_section_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            rows=[{'A':'阶段一'}, {'C':'体位编号','D':'birads分类','E':'病灶对应帧'},
                  {'C':'2.25.123','D':'2类 10-20'},
                  {'A':'阶段二'}, {'C':'2.25.124','D':'3类 30-40'}]
            self.write_lesion_workbook(root,rows)
            self.assertEqual(workbook_notes(root/'segmentations.xlsx'),{'2.25.123':'2类 10-20'})
            rows += [{'A':'阶段三'}, {'C':'体位编号','D':'审核结果','E':'备注'},
                     {'C':'2.25.123','D':'4A类','E':'50-60'}]
            self.write_lesion_workbook(root,rows)
            self.assertEqual(workbook_notes(root/'segmentations.xlsx',True),{'2.25.123':('50-60','4A类')})

    def test_lesion_layout_rejects_duplicate_and_incomplete_headers(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            rows=[{'C':'体位编号','D':'birads分类','E':'病灶对应帧'},
                  {'C':'2.25.123','D':'2类 10-20'}, {'C':'2.25.123','D':'1类'}]
            self.write_lesion_workbook(root,rows)
            with self.assertRaisesRegex(ValueError,'重复病例'):
                workbook_notes(root/'segmentations.xlsx')
            rows[0].pop('E')
            self.write_lesion_workbook(root,rows[:2])
            with self.assertRaises(ValueError):
                workbook_notes(root/'segmentations.xlsx')

    def test_image_filename_fallback_and_original_priority(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for uid in ('123456789012345','2.25.123'):
                directory=root/uid;directory.mkdir()
                case=Case(uid,directory)
                original=directory/(uid+'_ORI.dcm')
                plain=directory/(uid+'.dcm')
                self.assertEqual(case.image,original)
                plain.write_bytes(b'plain fixture')
                self.assertEqual(case.image,plain)
                original.write_bytes(b'original fixture')
                self.assertEqual(case.image,original)
                self.assertEqual(plain.read_bytes(),b'plain fixture')
                self.assertEqual(original.read_bytes(),b'original fixture')

    def test_discover_numeric_plain_dcm_without_doctor_or_table_record(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.write_lesion_workbook(root,[
                {'C':'体位编号','D':'birads分类','E':'病灶对应帧'},
                {'C':'123456789012345','D':'2类 10-20'}])
            for uid in ('123456789012345','123456789012346'):
                directory=root/uid;directory.mkdir()
                (directory/(uid+'.dcm')).write_bytes(b'fixture')
            cases={c.uid:c for c in discover_cases(root)}
            self.assertEqual(len(cases),2)
            for case in cases.values():
                self.assertEqual(case.image.name,case.uid+'.dcm')
                self.assertFalse(any('缺少文件' in w and '.dcm' in w for w in case.warnings))
                self.assertIn('缺少文件：Segmentation.nrrd',case.warnings)
            self.assertIn('表格中缺少该病例',cases['123456789012346'].warnings)

    def test_review_category_fallback(self):
        for category in ('4a','4A类','BI-RADS 4A','审核结果：４Ａ类'):
            ranges,warnings = parse_ranges('10-15-20,30-40',100,category)
            self.assertFalse(warnings)
            self.assertEqual([r.category for r in ranges],['4A类','4A类'])
            self.assertEqual(ranges[0].middle,15)
        ranges,warnings = parse_ranges('2类 10-20/30-40',100,'4A')
        self.assertFalse(warnings)
        self.assertEqual([r.category for r in ranges],['2类','2类'])
        self.assertTrue(parse_ranges('10-20',100,'未知')[1])

    def test_workbook_adjacent_review_category(self):
        # AA remarks must read Z, rather than another review-like column.
        xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        xml += '<row r="1"><c r="A1" t="inlineStr"><is><t>阶段三</t></is></c><c r="Z1" t="inlineStr"><is><t>审核结果</t></is></c><c r="AA1" t="inlineStr"><is><t>备注</t></is></c></row>'
        xml += '<row r="2"><c r="C2" t="inlineStr"><is><t>2.25.123</t></is></c><c r="Y2" t="inlineStr"><is><t>2类</t></is></c><c r="Z2" t="inlineStr"><is><t>4A类</t></is></c><c r="AA2" t="inlineStr"><is><t>10-15-20,30-40</t></is></c></row></sheetData></worksheet>'
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with zipfile.ZipFile(root/'segmentations.xlsx','w') as output:
                output.writestr('xl/worksheets/sheet1.xml',xml)
            case=discover_cases(root)[0]
            self.assertEqual(case.note,'10-15-20,30-40')
            self.assertEqual(case.review_category,'4A类')
            self.assertEqual([r.category for r in case.ranges],['4A类','4A类'])
    def test_three_frame_colon_notes(self):
        ranges, warnings = parse_ranges('2:90-80-70,85-75-65',100)
        self.assertFalse(warnings)
        self.assertEqual([(r.category,r.start,r.middle,r.end) for r in ranges],
                         [('2类',70,80,90),('2类',65,75,85)])
        self.assertTrue(all(r.contains(75) for r in ranges))

    def test_three_frame_mixed_notes_and_warnings(self):
        ranges, warnings = parse_ranges('2类 10-20/4A：30-35-40，45-50-55/3类：60-70',100)
        self.assertFalse(warnings)
        self.assertEqual([r.category for r in ranges],['2类','4A类','4A类','3类'])
        self.assertEqual([r.middle for r in ranges],[None,35,50,None])
        self.assertTrue(parse_ranges('2:10-30-20',100)[1])
        self.assertTrue(parse_ranges('2:10-110-20',100)[1])
        self.assertEqual(len(parse_ranges('2:10-15-20-25')[0]),0)
    def test_class_one_without_ranges_is_exempt(self):
        for note in ('', '  ', '1类', ' 1 类 ', '１类', '1类/'):
            self.assertTrue(Case('synthetic',Path('.'),note,in_workbook=True).annotation_exempt)
        for note in ('11类','2类','4A类','1类 10-20','2类 10-20'):
            self.assertFalse(Case('synthetic',Path('.'),note,in_workbook=True).annotation_exempt)
        self.assertFalse(Case('synthetic',Path('.'),'1类',in_workbook=False).annotation_exempt)
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

    def test_atomic_windows_flush_requires_writable_handle(self):
        # Emulate Windows' writable-handle requirement on any test platform.
        original_open = Path.open
        original_fsync = os.fsync
        streams = {}
        flushed = []
        def tracked_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            streams[stream.fileno()] = stream
            return stream
        def windows_fsync(fd):
            stream = streams[fd]
            if not stream.writable():
                raise OSError(9, "Bad file descriptor")
            flushed.append(stream.name)
            original_fsync(fd)
        with tempfile.TemporaryDirectory(prefix="中文 病例-") as d:
            target = Path(d)/"Segment.nrrd"
            with patch.object(Path, "open", tracked_open), patch("BreastContourLib.core.os.fsync", windows_fsync):
                self.assertIsNone(atomic_save(target, lambda p: p.write_bytes(b"first"),
                    lambda p: self.assertEqual(p.read_bytes(), b"first")))
                self.assertEqual(target.read_bytes(), b"first")
                backup = atomic_save(target, lambda p: p.write_bytes(b"second"),
                    lambda p: self.assertEqual(p.read_bytes(), b"second"))
                self.assertEqual(target.read_bytes(), b"second")
                self.assertEqual(backup.read_bytes(), b"first")
            self.assertEqual(len(flushed), 3)
            self.assertFalse(list(Path(d).glob(".Segment-*")))

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
