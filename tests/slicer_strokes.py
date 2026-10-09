"""Native mouse strokes at fractional diameters; temporary work, no source writes."""
from pathlib import Path
import sys
import tempfile
import time
import math
import json
import traceback
import numpy as np
import slicer
import vtk
import qt
sys.stdout = sys.stderr = open('/tmp/ohslicer-shortcuts-python.log', 'w', buffering=1)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'BreastContour'))
from BreastContourLib.core import discover_cases
from BreastContourLib.runtime import named_segments


def run():
    slicer.util.selectModule('BreastContour')
    widget = slicer.modules.breastcontour.widgetRepresentation().self()
    source = discover_cases('/home/caesar/Projects/ohslicer')[2]
    reports = []
    with tempfile.TemporaryDirectory(prefix='breast-contour-strokes-') as d:
        folder = Path(d)
        (folder/source.image.name).symlink_to(source.image)
        (folder/'Segmentation.nrrd').symlink_to(source.doctor)
        from BreastContourLib.core import Case
        widget.cases = [Case(source.uid, folder, source.note, source.ranges)]
        widget.case_combo.addItem(source.uid)
        widget.select_case(0)
        assert widget.session.case
        assert (folder/'Segment.nrrd').exists(), 'work file not created on open'
        assert not widget.session.dirty
        assert set(named_segments(widget.session.work)) == set(named_segments(widget.session.doctor))
        for name, sid in named_segments(widget.session.work).items():
            doctor_sid = named_segments(widget.session.doctor)[name]
            assert widget.session.work.GetSegmentation().GetSegment(sid).GetColor() == widget.session.doctor.GetSegmentation().GetSegment(doctor_sid).GetColor()
            assert not widget.session.array(name).any()
        frame = int(widget.session.doctor_frames[0])
        widget.go_to_frame(frame)
        widget.doctor_visible.checked = False
        view = widget.slice_widget.sliceView()
        interactor = view.interactor()
        name = next(iter(named_segments(widget.session.work)))
        sid = named_segments(widget.session.work)[name]
        widget.editor.setCurrentSegmentID(sid)
        slicer.app.processEvents()
        # Rasterized circle near the center, through the native interactor event path.
        cx, cy = int(view.width/2), int(view.height/2)
        radius = min(view.width, view.height)//8
        points = [(int(cx+radius*math.cos(t)), int(cy+radius*math.sin(t)))
                  for t in np.linspace(0,2*math.pi,101)]
        for diameter in (1.0,1.5,2.0):
            widget.paint_size.value = diameter
            assert widget.editor.activeEffect().integerParameter('BrushPixelMode') == 0
            started = time.perf_counter()
            interactor.SetEventPosition(*points[0])
            interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
            interactor.InvokeEvent(vtk.vtkCommand.LeftButtonPressEvent)
            for point in points[1:]:
                interactor.SetEventPosition(*point)
                interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
            before_release = time.perf_counter()
            interactor.InvokeEvent(vtk.vtkCommand.LeftButtonReleaseEvent)
            slicer.app.processEvents()
            ended = time.perf_counter()
            pixels = widget.session.array(name)
            count = int(pixels[frame].sum())
            assert count > 0, 'native mouse stroke produced no pixels'
            assert not np.any(np.delete(np.any(pixels,axis=(1,2)),frame)), 'cross-frame edit'
            del pixels
            key = qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_Z, qt.Qt.ControlModifier)
            qt.QApplication.sendEvent(slicer.util.mainWindow(), key)
            slicer.app.processEvents()
            assert not widget.session.array(name).any(), 'undo did not restore empty work'
            reports.append({'diameter':diameter,'pixels':count,'gesture_seconds':ended-started,
                            'release_seconds':ended-before_release})
            print(reports[-1],flush=True)
        widget.detach()
        widget.session.close()
    Path('/tmp/ohslicer-shortcuts-report.json').write_text(json.dumps(reports,indent=2))
    print('NATIVE STROKES PASSED',flush=True)

try:
    run()
except Exception:
    traceback.print_exc()
    slicer.util.exit(1)
else:
    slicer.util.exit(0)
