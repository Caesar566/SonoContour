"""Exercise the real widget under a normal Slicer GUI, with temporary outputs."""
from pathlib import Path
import json
import os
import shutil
import sys
import tempfile
import traceback
sys.stdout = sys.stderr = open('/tmp/ohslicer-ui-python.log', 'w', buffering=1)
print('UI_TEST_SCRIPT_STARTED', flush=True)
Path('/tmp/ohslicer-ui-report.json').unlink(missing_ok=True)

import numpy as np
import qt
import slicer
import vtk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"BreastContour"))
from BreastContourLib.core import discover_cases
from BreastContourLib.runtime import named_segments


def click_dialog(label):
    def click():
        box = slicer.app.activeModalWidget()
        print('DIALOG_LOOKUP', label, box, flush=True)
        if box:
            for button in box.buttons():
                print('DIALOG_BUTTON', button.text, flush=True)
                if button.text == label:
                    button.click()
                    print('DIALOG_CLICKED', label, flush=True)
                    return
        raise AssertionError("未出现对话框按钮：" + label)
    qt.QTimer.singleShot(40, click)


def run():
    print('MODULE', slicer.modules.breastcontour, flush=True)
    slicer.util.selectModule("BreastContour")
    widget = slicer.modules.breastcontour.widgetRepresentation().self()
    assert widget.editor
    data_root = Path(os.environ.get('BREAST_CONTOUR_DATA', str(Path(__file__).resolve().parents[1])))
    cases = discover_cases(data_root)
    info_original = slicer.util.infoDisplay
    error_original = slicer.util.errorDisplay
    messages = []
    def error(message, **kwargs):
        raise AssertionError(str(message))
    slicer.util.infoDisplay = lambda message, **kwargs: messages.append(str(message))
    slicer.util.errorDisplay = error
    try:
        with tempfile.TemporaryDirectory(prefix='breast-contour-ui-') as d:
            root = Path(d)
            shutil.copy2(data_root/'segmentations.xlsx', root/'segmentations.xlsx')
            for source in cases:
                folder = root/source.uid
                folder.mkdir()
                (folder/source.image.name).symlink_to(source.image)
                (folder/'Segmentation.nrrd').symlink_to(source.doctor)
                shutil.copy2(source.output, folder/'Segment.nrrd')
            widget.root_path.currentPath = str(root)
            widget.paint_size.value = 1.5
            widget.erase_size.value = 15
            widget.import_root()
            assert widget.current_index == 0 and widget.session.case
            slicer.app.processEvents()
            print('UI_LOADED', flush=True)
            assert slicer.util.findChild(widget.editor, 'OptionsGroupBox').isHidden()
            assert widget.editor.activeEffect().name == 'Paint'
            assert abs(widget.editor.activeEffect().doubleParameter('BrushAbsoluteDiameter')/widget.session.volume.GetSpacing()[0]-1.5)<0.01
            widget.editor.setActiveEffectByName('Erase')
            assert slicer.util.findChild(widget.editor, 'OptionsGroupBox').isHidden()
            assert abs(widget.editor.activeEffect().doubleParameter('BrushAbsoluteDiameter')/widget.session.volume.GetSpacing()[0]-15)<0.01
            widget.editor.setActiveEffectByName('Paint')
            assert widget.paint_size.value == 1.5 and widget.erase_size.value == 15
            widget.paint_size.value = 5
            widget.editor.setActiveEffectByName('Erase')
            widget.erase_size.value = 21
            widget.editor.setActiveEffectByName('Paint')
            assert abs(widget.editor.activeEffect().doubleParameter('BrushAbsoluteDiameter')/widget.session.volume.GetSpacing()[0]-5)<0.01
            assert int(slicer.app.settings().value('BreastContour/ErasePixels')) == 21
            assert widget.editor.activeEffect().integerParameter('BrushSphere') == 0
            assert widget.editor.activeEffect().integerParameter('BrushDiameterIsRelative') == 0
            for diameter in (1.0, 1.5, 2.0):
                widget.paint_size.value = diameter
                assert widget.editor.activeEffect().integerParameter('BrushPixelMode') == 0
                assert abs(widget.editor.activeEffect().doubleParameter('BrushAbsoluteDiameter')/widget.session.volume.GetSpacing()[0]-diameter)<0.001
            widget.doctor_visible.checked = False
            assert not widget.session.doctor.GetDisplayNode().GetVisibility()
            widget.doctor_visible.checked = True
            assert widget.session.doctor.GetDisplayNode().GetVisibility()
            print('BRUSH_SIZES_PASSED', flush=True)
            frame_count = widget.session.shape[0]
            for frame in [0, min(10,frame_count-1), min(25,frame_count-1), frame_count-1]:
                widget.go_to_frame(frame)
                assert widget._frame == frame
                assert widget.frame_spin.value == frame
                assert f'第 {frame+1}/{frame_count}' in widget.frame_label.text
            target_frame = min(26,frame_count-1)
            matrix = vtk.vtkMatrix4x4()
            widget.session.volume.GetIJKToRASMatrix(matrix)
            point = matrix.MultiplyPoint((0,0,target_frame,1))
            plane = widget.slice_widget.mrmlSliceNode().GetSliceToRAS()
            offset = sum(point[i]*plane.GetElement(i,2) for i in range(3))
            widget.slice_widget.sliceLogic().SetSliceOffset(offset)
            assert widget._frame == target_frame, widget._frame
            print('NAVIGATION_PASSED', flush=True)
            # Native Paint and undo can edit former doctor frames in the work node.
            name = next(iter(named_segments(widget.session.work)))
            sid = named_segments(widget.session.work)[name]
            widget.editor.setCurrentSegmentID(sid)
            effect = widget.editor.activeEffect()
            modifier = effect.defaultModifierLabelmap()
            image = np.zeros(widget.session.shape, dtype=np.uint8)
            locked = int(widget.session.doctor_frames[0])
            editable = next(k for k in range(500) if k not in widget.session.doctor_frames)
            image[locked, 5, 5] = image[editable, 5, 5] = 1
            # Update VTK modifier data in the volume geometry.
            from vtk.util.numpy_support import numpy_to_vtk
            modifier.SetExtent(widget.session.volume.GetImageData().GetExtent())
            matrix = vtk.vtkMatrix4x4()
            widget.session.volume.GetIJKToRASMatrix(matrix)
            modifier.SetGeometryFromImageToWorldMatrix(matrix)
            modifier.GetPointData().SetScalars(numpy_to_vtk(image.ravel(), deep=True, array_type=vtk.VTK_UNSIGNED_CHAR))
            before_locked = int(widget.session.array(name)[locked,5,5])
            effect.saveStateForUndo()
            effect.modifySelectedSegmentByLabelmap(modifier, effect.ModificationModeAdd)
            slicer.app.processEvents()
            pixels = widget.session.array(name)
            assert pixels[editable,5,5] == 1
            assert pixels[locked,5,5] == 1
            del pixels, image
            widget.editor.undo()
            slicer.app.processEvents()
            assert widget.session.array(name)[locked,5,5] == before_locked
            print('PAINT_AND_UNDO_PASSED', flush=True)
            # Cancel must preserve the current case and dirty state.
            widget.session.dirty = True
            print('TEST_CANCEL_SWITCH', flush=True)
            click_dialog('取消')
            widget.select_case(1)
            print('CANCEL_SWITCH_PASSED', flush=True)
            assert widget.current_index == 0 and widget.session.dirty
            click_dialog('返回修正')
            assert not widget.save()
            print('SAVE_BACK_PASSED', flush=True)
            assert widget.session.dirty
            # Route a real Ctrl+S key event through the installed event filter.
            click_dialog('仍然保存')
            key = qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_S, qt.Qt.ControlModifier)
            qt.QApplication.sendEvent(slicer.util.mainWindow(), key)
            assert not widget.session.dirty
            assert list((root/cases[0].uid).glob('Segment.backup-*'))
            # A clean switch loads the next case without a discard dialog.
            widget.select_case(1)
            assert widget.current_index == 1
            widget.select_case(0)
            assert widget.current_index == 0
            print('SWITCH_SAVE_SHORTCUT_PASSED', flush=True)
            widget.go_to_frame(138)
            slicer.app.processEvents()
            slicer.util.mainWindow().grab().save('/tmp/ohslicer-ui.png')
            widget.detach()
            widget.session.close()
    finally:
        slicer.util.infoDisplay = info_original
        slicer.util.errorDisplay = error_original
    Path('/tmp/ohslicer-ui-report.json').write_text(json.dumps({
        'slicer':slicer.app.applicationVersion,'brush_sizes':'passed','navigation':'passed',
        'editable_doctor_frame':'passed','undo':'passed','fractional_brush':'passed','doctor_visibility':'passed','cancel_switch':'passed',
        'save_shortcut':'passed','save_return_to_edit':'passed','case_switch':'passed'},indent=2))
    print('ALL UI TESTS PASSED', flush=True)


def start():
    try:
        run()
    except Exception:
        traceback.print_exc()
        slicer.util.exit(1)
    else:
        slicer.util.exit(0)

start()
