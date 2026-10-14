"""Reference visibility recovery with synthetic data; never reads patient files."""
from pathlib import Path
import sys
import traceback
import numpy as np
import slicer
import vtk
import qt
from vtk.util.numpy_support import vtk_to_numpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'BreastContour'))
from BreastContourLib.runtime import CaseSession, remove_node


def run():
    session = CaseSession()
    try:
        session.shape = (3,40,50)
        session.volume = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLScalarVolumeNode')
        slicer.util.updateVolumeFromArray(session.volume,np.zeros(session.shape,dtype=np.uint8))
        for field in ('doctor','work'):
            node = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLSegmentationNode')
            node.CreateDefaultDisplayNodes()
            node.SetReferenceImageGeometryParameterFromVolumeNode(session.volume)
            setattr(session,field,node)
            for index in range(2):
                sid = node.GetSegmentation().AddEmptySegment('s'+str(index),'s'+str(index))
                pixels = np.zeros(session.shape,dtype=np.uint8)
                if field == 'doctor':
                    pixels[1,10+index*10,10:30] = 1
                slicer.util.updateSegmentBinaryLabelmapFromArray(pixels,node,sid,session.volume)
        originals = {name:session.array(name,session.doctor).copy() for name in ('s0','s1')}
        session.set_doctor_visibility(True)
        def check():
            display = session.doctor.GetDisplayNode()
            assert display.GetVisibility() and display.GetVisibility2D()
            assert display.GetVisibility2DFill() and display.GetOpacity() == 1.0
            assert abs(display.GetOpacity2DFill()-0.35) < 1e-6
            assert display.GetNumberOfViewNodeIDs() == 0
            assert display.GetPreferredDisplayRepresentationName2D() == 'Binary labelmap'
            for sid in ('s0','s1'):
                assert display.GetSegmentVisibility(sid)
                assert display.GetSegmentVisibility2DFill(sid)
                assert display.GetSegmentOpacity2DFill(sid) == 1.0
        # Reproduce the old failure: global visibility does not restore segments.
        session.protect_doctor_display = False
        display = session.doctor.GetDisplayNode()
        display.SetSegmentVisibility('s0',False)
        display.SetVisibility(False);display.SetVisibility(True)
        assert not display.GetSegmentVisibility('s0')
        session.set_doctor_visibility(False);session.set_doctor_visibility(True)
        check()
        session.protect_doctor_display = True
        mutations = [lambda d:d.SetVisibility(False), lambda d:d.SetVisibility2D(False),
            lambda d:d.SetVisibility2DFill(False),lambda d:d.SetOpacity(0),
            lambda d:d.SetOpacity2DFill(0),lambda d:d.SetSegmentVisibility('s0',False),
            lambda d:d.SetSegmentVisibility2DFill('s1',False),
            lambda d:d.SetSegmentOpacity2DFill('s0',0),
            lambda d:d.AddViewNodeID('unrelated-view'),
            lambda d:d.SetPreferredDisplayRepresentationName2D('Closed surface')]
        for mutate in mutations:
            mutate(session.doctor.GetDisplayNode())
            slicer.app.processEvents()
            check()
        session.set_doctor_visibility(False)
        assert not display.GetVisibility()
        display.SetVisibility(True)
        slicer.app.processEvents()
        assert not display.GetVisibility(), 'Checkbox-off must remain hidden'
        session.set_doctor_visibility(True);check()
        # Recovery never exports or modifies full-volume labelmaps.
        original_array = session.array
        session.array = lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('display exported pixels'))
        display.SetOpacity(0);slicer.app.processEvents();check()
        session.array = original_array
        assert not session.dirty
        for name, pixels in originals.items():
            np.testing.assert_array_equal(session.array(name,session.doctor),pixels)
            assert not session.array(name).any()
        session.protect_doctor_display = False
        display.SetVisibility(False)
        assert not display.GetVisibility(), 'Other modules must retain display control'
        session.protect_doctor_display = True
        session.set_doctor_visibility(True);check()
        if slicer.util.mainWindow():
            slicer.util.mainWindow().setFixedSize(1000,800)
            manager = slicer.app.layoutManager()
            manager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
            view = manager.sliceWidget('Red')
            slicer.util.setSliceViewerLayers(background=session.volume)
            view.sliceLogic().FitSliceToAll()
            matrix = view.mrmlSliceNode().GetSliceToRAS()
            view.sliceLogic().SetSliceOffset(matrix.GetElement(2,2))
            loop = qt.QEventLoop()
            qt.QTimer.singleShot(300,loop.quit)
            loop.exec()
            def capture():
                slicer.app.processEvents()
                window = view.sliceView().renderWindow()
                window.Render()
                capture = vtk.vtkWindowToImageFilter()
                capture.SetInput(window)
                capture.ReadFrontBufferOff()
                capture.Update()
                return vtk_to_numpy(capture.GetOutput().GetPointData().GetScalars()).copy()
            session.set_doctor_visibility(False)
            hidden = capture()
            session.set_doctor_visibility(True)
            visible = capture()
            assert np.any(hidden != visible), 'Doctor pixels were not rendered'
            display.SetSegmentVisibility('s0',False)
            display.SetVisibility2D(False)
            display.SetSegmentOpacity2DFill('s1',0)
            np.testing.assert_array_equal(capture(),visible)
            print('REFERENCE RENDERED PIXELS RECOVERED')
        print('REFERENCE DISPLAY RECOVERY PASSED')
    finally:
        session.close()


try:
    run()
except Exception:
    traceback.print_exc();slicer.util.exit(1)
else:
    slicer.util.exit(0)
