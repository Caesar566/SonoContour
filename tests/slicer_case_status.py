"""Case labels follow empty expected frames, not the existence of an NRRD."""
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch
import zipfile
from types import SimpleNamespace
import vtk
import numpy as np
import slicer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'BreastContour'))
from BreastContourLib.core import Case, parse_ranges
from BreastContourLib.runtime import CaseSession, annotation_status, saved_annotation_status


def run():
    widget = slicer.modules.breastcontour.widgetRepresentation().self()
    session = CaseSession()
    with tempfile.TemporaryDirectory(prefix='case-status-') as folder:
        case = Case('synthetic',Path(folder),'2类 0-2')
        case.ranges = parse_ranges(case.note)[0]
        assert saved_annotation_status(case) == '未开始'
        try:
            session.case = case
            session.shape = (4,30,40)
            session.volume = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLScalarVolumeNode')
            slicer.util.updateVolumeFromArray(session.volume,np.zeros(session.shape,dtype=np.uint8))
            session.work = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLSegmentationNode')
            session.work.CreateDefaultDisplayNodes()
            session.work.SetReferenceImageGeometryParameterFromVolumeNode(session.volume)
            for sid in ('first','second'):
                session.work.GetSegmentation().AddEmptySegment(sid,sid)
            assert annotation_status(session.work,case.ranges,4) == '待确认'
            session.save()
            assert saved_annotation_status(case) == '待确认'
            # Different segments may cover different expected frames.
            a = np.zeros(session.shape,dtype=np.uint8)
            a[0,10,10:20] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(a,session.work,'first',session.volume)
            a[:] = 0;a[2,10,10:20] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(a,session.work,'second',session.volume)
            assert annotation_status(session.work,case.ranges,4) == '待确认'
            a[1,10,10:20] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(a,session.work,'second',session.volume)
            assert annotation_status(session.work,case.ranges,4) == '已有结果'
            assert saved_annotation_status(case) == '待确认', 'Unsaved edits must not change disk status'
            session.save()
            complete_file = case.output.read_bytes()
            count = slicer.mrmlScene.GetNumberOfNodes()
            for _ in range(3):
                assert saved_annotation_status(case) == '已有结果'
                assert slicer.mrmlScene.GetNumberOfNodes() == count
            # Reuse the real UI check/save handlers against synthetic data.
            original_session = widget.session
            widget.session = session
            widget.cases = [case]
            widget.case_combo.blockSignals(True)
            widget.case_combo.clear();widget.case_combo.addItem('synthetic [已有结果]')
            widget.case_combo.blockSignals(False)
            widget._case_statuses = {case.uid:'已有结果'}
            try:
                widget.run_check()
                assert '[已有结果]' in widget.case_combo.itemText(0)
                a[1] = 0
                slicer.util.updateSegmentBinaryLabelmapFromArray(a,session.work,'second',session.volume)
                widget.run_check()
                assert '[待确认]' in widget.case_combo.itemText(0)
                assert widget._case_statuses[case.uid] == '已有结果'
                # Save permits incomplete work; automatically approve its dialog.
                import qt
                def approve():
                    dialog = slicer.app.activeModalWidget()
                    if dialog:
                        for button in dialog.buttons():
                            if button.text == '仍然保存':
                                button.click();return
                qt.QTimer.singleShot(50,approve)
                assert widget.save()
                assert '[待确认]' in widget.case_combo.itemText(0)
                assert widget._case_statuses[case.uid] == '待确认'
                assert saved_annotation_status(case) == '待确认'
                old_note = case.note
                case.note = '  '
                case.in_workbook = True
                case.ranges = []
                assert saved_annotation_status(case) == '无需标注'
                widget.run_check()
                assert '[无需标注]' in widget.case_combo.itemText(0)
                annotation = vtk.vtkCornerAnnotation()
                original_view = getattr(widget,'slice_widget',None)
                widget.slice_widget = SimpleNamespace(sliceView=lambda:SimpleNamespace(
                    cornerAnnotation=lambda:annotation,scheduleRender=lambda:None))
                try:
                    widget.update_frame_text()
                    assert '无需标注' in widget.frame_label.text
                    assert '无需标注' in annotation.GetText(vtk.vtkCornerAnnotation.UpperRight)
                finally:
                    if original_view is None:
                        del widget.slice_widget
                    else:
                        widget.slice_widget = original_view
                # Existing pixels may still generate topology warnings, but
                # exemption is determined by the explicitly blank worksheet note.
                assert widget.checked_case_status(session.check()) == '无需标注'
                case.note = old_note
                case.ranges = parse_ranges(case.note)[0]
            finally:
                widget.editor.setActiveEffectByName('')
                widget.editor.setSegmentationNode(None)
                widget.editor.setSourceVolumeNode(None)
                widget.editor.setMRMLSegmentEditorNode(widget.empty_editor_node)
                widget.session = original_session
            # Import must show the correct state even before a case is opened.
            root = Path(folder)/'import'
            root.mkdir()
            for uid, payload in (('2.25.101',case.output.read_bytes()),
                                 ('2.25.102',complete_file),('2.25.103',None),
                                 ('2.25.104',None),('2.25.105',complete_file),
                                 ('2.25.106',None),('2.25.107',None),('2.25.108',complete_file)):
                directory = root/uid;directory.mkdir()
                if payload is not None:
                    (directory/'Segment.nrrd').write_bytes(payload)
            xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            xml += '<row r="1"><c r="A1" t="inlineStr"><is><t>阶段三</t></is></c><c r="E1" t="inlineStr"><is><t>备注</t></is></c></row>'
            for row, uid in enumerate(('2.25.101','2.25.102','2.25.103','2.25.104','2.25.105','2.25.107','2.25.108'),2):
                note = '2类 0-2' if row < 5 else ('  ' if row < 7 else '1类')
                xml += f'<row r="{row}"><c r="C{row}" t="inlineStr"><is><t>{uid}</t></is></c><c r="E{row}" t="inlineStr"><is><t>{note}</t></is></c></row>'
            xml += '</sheetData></worksheet>'
            with zipfile.ZipFile(root/'segmentations.xlsx','w') as workbook:
                workbook.writestr('xl/worksheets/sheet1.xml',xml)
            widget.root_path.currentPath = str(root)
            with patch.object(widget,'select_case',lambda index:None), patch.object(slicer.util,'errorDisplay',side_effect=AssertionError):
                widget.import_root()
            for index,state in enumerate(('待确认','已有结果','未开始','无需标注','无需标注','未开始','无需标注','无需标注')):
                assert '['+state+']' in widget.case_combo.itemText(index)
            assert '阶段三表格中缺少该病例' in widget.cases[5].warnings
            assert not widget.cases[5].annotation_exempt
            for item in widget.cases[3:5]+widget.cases[6:8]:
                assert item.annotation_exempt and not item.ranges
                assert '没有可用的病灶范围' not in item.warnings
        finally:
            session.close()
    print('CASE STATUS TESTS PASSED')


try:
    run()
except Exception:
    traceback.print_exc();slicer.util.exit(1)
else:
    slicer.util.exit(0)
