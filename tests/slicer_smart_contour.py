"""Real Slicer events; synthetic pixels and a read-only real case in temp dirs."""
from pathlib import Path
import sys
import tempfile
import json
import traceback
import time
import hashlib
import numpy as np
import qt
import slicer
import vtk
from vtk.util.numpy_support import vtk_to_numpy
sys.stdout=sys.stderr=open('/tmp/ohslicer-smart-python.log','w',buffering=1)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'BreastContour'))
from BreastContourLib.core import Case, parse_ranges, discover_cases
from BreastContourLib.runtime import named_segments, remove_node


def wait_events(ms=80):
    loop=qt.QEventLoop();qt.QTimer.singleShot(ms,loop.quit);loop.exec()


def xy_for(widget,y,x):
    matrix=vtk.vtkMatrix4x4();widget.session.volume.GetIJKToRASMatrix(matrix)
    ras=matrix.MultiplyPoint((x,y,widget._frame,1))
    inverse=vtk.vtkMatrix4x4();inverse.DeepCopy(widget.slice_widget.mrmlSliceNode().GetXYToRAS());inverse.Invert()
    xy=inverse.MultiplyPoint(ras)
    return int(round(xy[0])),int(round(xy[1]))


def event_at(widget,y,x,event):
    interactor=widget.slice_widget.sliceView().interactor()
    interactor.SetEventPosition(*xy_for(widget,y,x));interactor.InvokeEvent(event)


def undo():
    qt.QApplication.sendEvent(slicer.util.mainWindow(),qt.QKeyEvent(qt.QEvent.KeyPress,qt.Qt.Key_Z,qt.Qt.ControlModifier))
    slicer.app.processEvents()


def space_key(widget, kind, repeat=False):
    qt.QApplication.sendEvent(widget.slice_widget.sliceView(),
        qt.QKeyEvent(kind, qt.Qt.Key_Space, qt.Qt.NoModifier, " ", repeat, 1))
    slicer.app.processEvents()


def temporary_smart(widget):
    widget.go_to_frame(2)
    widget.editor.setCurrentSegmentID('first')
    for previous in ('Paint', 'Erase', '', 'SmartContour'):
        widget.editor.setActiveEffectByName(previous);widget.on_effect_changed()
        event_at(widget,60,65,vtk.vtkCommand.MouseMoveEvent)
        space_key(widget,qt.QEvent.KeyPress);wait_events()
        assert widget._space_smart
        assert widget.editor.activeEffect().name == 'SmartContour'
        effect = widget.editor.activeEffect().self()
        assert effect.candidate is not None and effect.candidate.valid
        space_key(widget,qt.QEvent.KeyRelease,True)
        space_key(widget,qt.QEvent.KeyPress,True)
        assert widget._space_smart and widget._space_restore_effect == previous
        frame = widget._frame
        threshold = widget.smart_tolerance.value
        event_at(widget,60,65,vtk.vtkCommand.MouseWheelForwardEvent);wait_events()
        assert widget._frame == frame and widget.smart_tolerance.value == min(100,threshold+1)
        before = widget.session.array('first').copy()
        event_at(widget,60,65,vtk.vtkCommand.LeftButtonPressEvent)
        event_at(widget,60,65,vtk.vtkCommand.LeftButtonReleaseEvent)
        assert widget.session.array('first')[2].any()
        undo();np.testing.assert_array_equal(widget.session.array('first'),before)
        space_key(widget,qt.QEvent.KeyRelease)
        assert not widget._space_smart and effect.candidate is None
        restored = widget.editor.activeEffect()
        assert (restored.name if restored else '') == previous
    widget.editor.setActiveEffectByName('Erase');widget.on_effect_changed()
    space_key(widget,qt.QEvent.KeyPress)
    qt.QApplication.sendEvent(slicer.util.mainWindow(),qt.QEvent(qt.QEvent.WindowDeactivate))
    assert not widget._space_smart and widget.editor.activeEffect().name == 'Erase'
    # Hover activation works even when a panel button still owns focus.
    view = widget.slice_widget.sliceView()
    old_pointer = qt.QCursor.pos()
    try:
        qt.QCursor.setPos(view.mapToGlobal(qt.QPoint(view.width//2,view.height//2)))
        wait_events()
        assert view.rect.contains(view.mapFromGlobal(qt.QCursor.pos()))
        widget.check_button.setFocus()
        qt.QApplication.sendEvent(widget.check_button,
            qt.QKeyEvent(qt.QEvent.KeyPress,qt.Qt.Key_Space,qt.Qt.NoModifier))
        assert widget._space_smart
        space_key(widget,qt.QEvent.KeyRelease)
        assert widget.editor.activeEffect().name == 'Erase'
        widget.frame_spin.setFocus()
        qt.QApplication.sendEvent(widget.frame_spin,
            qt.QKeyEvent(qt.QEvent.KeyPress,qt.Qt.Key_Space,qt.Qt.NoModifier))
        assert not widget._space_smart
    finally:
        qt.QCursor.setPos(old_pointer)
    # Ordinary spaces in controls must not switch tools.
    qt.QApplication.sendEvent(widget.frame_spin,
        qt.QKeyEvent(qt.QEvent.KeyPress,qt.Qt.Key_Space,qt.Qt.NoModifier))
    assert not widget._space_smart
    widget.editor.setActiveEffectByName('Paint');widget.on_effect_changed()
    view.setFocus()
    old = widget._frame
    event_at(widget,60,65,vtk.vtkCommand.MouseWheelBackwardEvent);slicer.app.processEvents()
    assert widget._frame != old
    space_key(widget,qt.QEvent.KeyPress)
    widget.exit()
    assert not widget._space_smart and widget.editor.activeEffect() is None
    widget.enter()


def bind(widget):
    widget.editor.setMRMLSegmentEditorNode(widget.session.editor_node)
    widget.editor.setSegmentationNode(widget.session.work)
    widget.editor.setSourceVolumeNode(widget.session.volume)
    widget.editor.setCurrentSegmentID(next(iter(named_segments(widget.session.work).values())))
    widget._brush_observer=widget.session.editor_node.AddObserver(vtk.vtkCommand.ModifiedEvent,widget.brush_parameters_changed,-1.0)
    widget.annotation_panel.enabled=True
    widget.frame_spin.setRange(0,widget.session.shape[0]-1)
    widget.frame_slider.setRange(0,widget.session.shape[0]-1)
    widget.attach_view()
    widget.editor.setActiveEffectByName('SmartContour');widget.on_effect_changed()
    widget.smart_tolerance.value=15
    # Let initial layout/slice notifications finish before injecting hover.
    wait_events(250)
    return widget.editor.activeEffect().self()


def synthetic(widget,folder):
    session=widget.session
    session.case=Case('synthetic',folder,'2类 0-2');session.case.ranges=parse_ranges(session.case.note)[0]
    image=np.full((3,120,180),170,dtype=np.uint8)
    y,x=np.indices((120,180));mask=((y-60)/22)**2+((x-65)/30)**2<=1
    image[:,mask]=60;image[:,45:75,120:145]=60;image[:,5:10,5:10]=230
    session.shape=image.shape
    session.volume=slicer.mrmlScene.AddNewNodeByClass('vtkMRMLScalarVolumeNode')
    slicer.util.updateVolumeFromArray(session.volume,image);session.volume.SetSpacing(0.1,0.1,1)
    session.work=slicer.mrmlScene.AddNewNodeByClass('vtkMRMLSegmentationNode');session.work.CreateDefaultDisplayNodes()
    session.work.SetReferenceImageGeometryParameterFromVolumeNode(session.volume)
    sid=session.work.GetSegmentation().AddEmptySegment('first','first',(1,0,0))
    second=session.work.GetSegmentation().AddEmptySegment('second','second',(0,1,0))
    session.editor_node=slicer.mrmlScene.AddNewNodeByClass('vtkMRMLSegmentEditorNode');session._configure_mask()
    segmentation=session.work.GetSegmentation()
    session._observers.append((segmentation,segmentation.AddObserver(slicer.vtkSegmentation.SourceRepresentationModified,session._on_modified)))
    effect=bind(widget)
    assert effect.active
    assert not effect.icon().isNull()
    event_at(widget,60,65,vtk.vtkCommand.MouseMoveEvent);wait_events()
    assert effect.candidate is not None and effect.candidate.valid, effect.candidate.reason if effect.candidate else 'no preview'
    assert effect.actor.GetVisibility()
    from scipy import ndimage as ndi
    holes = ndi.binary_fill_holes(effect.candidate.contour) & ~effect.candidate.contour
    assert ndi.label(holes)[1] == 1
    assert effect.poly.GetNumberOfLines() == 0, 'Diagonal preview lines create false holes'
    assert effect.poly.GetNumberOfPolys() == int(effect.candidate.contour.sum())
    assert not session.array('first').any() and not session.dirty
    frame=widget._frame
    event_at(widget,60,65,vtk.vtkCommand.MouseWheelForwardEvent);wait_events()
    assert widget._frame==frame and widget.smart_tolerance.value==16
    event_at(widget,60,65,vtk.vtkCommand.MouseWheelBackwardEvent);wait_events()
    assert widget._frame==frame and widget.smart_tolerance.value==15
    expected=effect.candidate.contour.copy()
    event_at(widget,60,65,vtk.vtkCommand.LeftButtonPressEvent)
    event_at(widget,60,65,vtk.vtkCommand.LeftButtonReleaseEvent);slicer.app.processEvents()
    a=session.array('first');np.testing.assert_array_equal(a[0],expected)
    assert not a[1:].any() and not a[0,60,65]
    assert not session.array('second').any()
    assert session.dirty
    # Duplicate confirmation must not consume the next undo step.
    event_at(widget,60,65,vtk.vtkCommand.LeftButtonPressEvent)
    undo();assert not session.array('first').any()
    event_at(widget,60,65,vtk.vtkCommand.LeftButtonPressEvent)
    first=session.array('first').copy()
    event_at(widget,60,130,vtk.vtkCommand.MouseMoveEvent);wait_events()
    assert effect.candidate.valid
    event_at(widget,60,130,vtk.vtkCommand.LeftButtonPressEvent)
    both=session.array('first')
    assert both.sum()>first.sum() and np.all(both[first.astype(bool)])
    undo();np.testing.assert_array_equal(session.array('first'),first)
    # Pending hover must be invalidated by frame/segment changes.
    event_at(widget,60,65,vtk.vtkCommand.MouseMoveEvent)
    widget.go_to_frame(1);wait_events();assert effect.candidate is None and effect.xy is None
    event_at(widget,60,65,vtk.vtkCommand.MouseMoveEvent);wait_events();assert effect.candidate.valid
    widget.editor.setCurrentSegmentID(second);wait_events();assert effect.candidate is None
    event_at(widget,60,65,vtk.vtkCommand.MouseMoveEvent);wait_events();assert effect.candidate.valid
    interactor=widget.slice_widget.sliceView().interactor();interactor.SetKeySym('Escape');interactor.InvokeEvent(vtk.vtkCommand.KeyPressEvent)
    assert effect.candidate is None
    event_at(widget,60,65,vtk.vtkCommand.MouseMoveEvent);wait_events()
    interactor.InvokeEvent(vtk.vtkCommand.LeaveEvent);assert effect.candidate is None
    session.save();reopened=slicer.util.loadSegmentation(str(session.case.output))
    try:np.testing.assert_array_equal(session.array('first',reopened),session.array('first'))
    finally:remove_node(reopened)
    widget.editor.setActiveEffectByName('Paint');assert not effect.active and effect.actor is None
    previous = widget._frame
    event_at(widget,60,65,vtk.vtkCommand.MouseWheelForwardEvent)
    slicer.app.processEvents()
    assert widget._frame != previous, 'Paint wheel navigation was intercepted'
    widget.editor.setActiveEffectByName('SmartContour');widget.on_effect_changed()
    # Explicit check retains the active tool and clears its preview.
    widget.run_check()
    assert widget.editor.activeEffect().name == 'SmartContour'
    temporary_smart(widget)
    widget.detach();session.close()
    return {'native_hover':'passed','wheel_isolation':'passed','single_frame_contour_only':'passed','duplicate_undo':'passed','multi_tumor_add':'passed','frame_segment_escape_leave_cleanup':'passed','save_roundtrip':'passed','space_hold_restore_repeat_focus_and_confirm':'passed'}


def real_case(widget,folder):
    import os
    root=Path(os.environ.get('BREAST_CONTOUR_DATA',str(Path(__file__).resolve().parents[1])))
    source=discover_cases(root)[2]
    doctor_digest=hashlib.sha256(source.doctor.read_bytes()).hexdigest()
    (folder/source.image.name).symlink_to(source.image);(folder/'Segmentation.nrrd').symlink_to(source.doctor)
    widget.session.load(Case(source.uid,folder,source.note,source.ranges))
    effect=bind(widget)
    session=widget.session
    image=slicer.util.arrayFromVolume(session.volume)
    found=None
    # Search inside a doctor's outlined region, without changing doctor data.
    from scipy import ndimage as ndi
    for name,sid in named_segments(session.doctor).items():
        segment=session.doctor.GetSegmentation().GetSegment(sid)
        native_image=segment.GetRepresentation('Binary labelmap')
        if native_image is None or not native_image.GetNumberOfPoints():continue
        native=vtk_to_numpy(native_image.GetPointData().GetScalars()).reshape(native_image.GetDimensions()[::-1])
        x0,x1,y0,y1,z0,z1=native_image.GetExtent()
        for k in np.flatnonzero(np.any(native==segment.GetLabelValue(),axis=(1,2)))[:3]:
            pixels=native[k]==segment.GetLabelValue()
            interior=ndi.binary_fill_holes(pixels)&~pixels
            yy,xx=np.nonzero(interior if interior.any() else pixels)
            if not len(yy):continue
            y,x=int(np.median(yy))+y0,int(np.median(xx))+x0
            prepared=prepare_frame(image[k+z0])
            for threshold in (10,15,25,40,60):
                candidate=candidate_from_prepared(prepared,(y,x),threshold)
                if candidate.valid:found=(int(k+z0),y,x,threshold);break
            if found:break
        if found:break
    assert found,'no valid candidate near doctor reference on sample'
    frame,y,x,threshold=found;widget.go_to_frame(frame);widget.smart_tolerance.value=threshold
    event_at(widget,y,x,vtk.vtkCommand.MouseMoveEvent);wait_events(150)
    assert effect.candidate.valid,effect.candidate.reason
    expected=effect.candidate.contour.copy();preview_seconds=effect.last_preview_seconds
    started=time.perf_counter();event_at(widget,y,x,vtk.vtkCommand.LeftButtonPressEvent);slicer.app.processEvents();confirm_seconds=time.perf_counter()-started
    name=next(iter(named_segments(session.work)))
    np.testing.assert_array_equal(session.array(name)[frame],expected)
    session.save();reopened=slicer.util.loadSegmentation(str(session.case.output))
    try:np.testing.assert_array_equal(session.array(name,reopened),session.array(name))
    finally:remove_node(reopened)
    assert hashlib.sha256(source.doctor.read_bytes()).hexdigest()==doctor_digest
    widget.detach();session.close()
    return {'real_case_roundtrip':'passed','doctor_unchanged':True,'preview_seconds':preview_seconds,'confirm_seconds':confirm_seconds,'pixels':int(expected.sum()),'manual_boundary_acceptance':'pending'}


def run():
    slicer.util.selectModule('BreastContour')
    widget=slicer.modules.breastcontour.widgetRepresentation().self()
    # Import after normal widget startup to expose effect-loader collisions.
    global prepare_frame, candidate_from_prepared
    from BreastContourLib.smart_contour import prepare_frame, candidate_from_prepared
    original=slicer.util.errorDisplay
    settings = slicer.app.settings()
    original_tolerance = settings.value('BreastContour/SmartTolerance', None)
    slicer.util.errorDisplay=lambda message,**kwargs: (_ for _ in ()).throw(AssertionError(str(message)))
    try:
        with tempfile.TemporaryDirectory(prefix='smart-synthetic-') as d:report=synthetic(widget,Path(d))
        print('SYNTHETIC PASSED',report,flush=True)
        with tempfile.TemporaryDirectory(prefix='smart-real-') as d:report.update(real_case(widget,Path(d)))
        report['slicer']=slicer.app.applicationVersion
        Path('/tmp/ohslicer-smart-report.json').write_text(json.dumps(report,indent=2))
        print('SMART CONTOUR PASSED',report,flush=True)
    finally:
        slicer.util.errorDisplay=original
        if original_tolerance is None:
            settings.remove('BreastContour/SmartTolerance')
        else:
            settings.setValue('BreastContour/SmartTolerance', original_tolerance)

try:run()
except Exception:
    traceback.print_exc();slicer.util.exit(1)
else:slicer.util.exit(0)
