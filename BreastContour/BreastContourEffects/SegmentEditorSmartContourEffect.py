"""Hover-only preview and undoable single-slice pixel contour addition."""
from pathlib import Path
import numpy as np
import qt
import slicer
import vtk
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy
from SegmentEditorEffects import AbstractScriptedSegmentEditorEffect
from BreastContourLib.smart_contour import prepare_frame, candidate_from_prepared


class SegmentEditorSmartContourEffect(AbstractScriptedSegmentEditorEffect):
    def __init__(self, scriptedEffect):
        scriptedEffect.name = "SmartContour"
        scriptedEffect.title = "智能轮廓"
        super().__init__(scriptedEffect)
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(50)
        self.timer.connect("timeout()", self.refresh)
        self.active = False
        self.view = None
        self.xy = None
        self.cache_key = None
        self.prepared = None
        self.candidate = None
        self.candidate_key = None
        self.actor = None
        self.poly = None
        self.last_context = None
        self.message_callback = None
        self.threshold_callback = None
        self.tolerance = max(1,min(100,int(slicer.app.settings().value("BreastContour/SmartTolerance",15))))
        self.last_preview_seconds = 0
        self.parameter_observer = None

    def clone(self):
        import qSlicerSegmentationsEditorEffectsPythonQt as effects
        cloned = effects.qSlicerSegmentEditorScriptedEffect(None)
        cloned.setPythonSource(str(Path(__file__).resolve()).replace("\\", "/"))
        return cloned

    def icon(self):
        # Use an existing platform-independent Slicer icon.
        import SegmentEditorEffects
        path = Path(SegmentEditorEffects.__file__).parent / "Resources" / "Icons" / "Threshold.png"
        if path.is_file():
            return qt.QIcon(str(path))
        return slicer.app.style().standardIcon(qt.QStyle.SP_MessageBoxInformation)

    def helpText(self):
        return "鼠标放在低回声肿瘤区域预览；滚轮调整边界变化容忍度；左键追加轮廓；Esc 清除预览；Ctrl+Z 撤销。"

    def activate(self):
        self.active = True
        self.clear()
        node = self.scriptedEffect.parameterSetNode()
        if node is not None and self.parameter_observer is None:
            self.parameter_observer = (node, node.AddObserver(vtk.vtkCommand.ModifiedEvent, self.on_parameter_changed))

    def deactivate(self):
        self.active = False
        if self.parameter_observer is not None:
            node, tag = self.parameter_observer
            node.RemoveObserver(tag)
            self.parameter_observer = None
        self.clear()
        self.cache_key = None
        self.prepared = None
        if self.actor is not None and self.view is not None:
            self.scriptedEffect.removeActor2D(self.view, self.actor)
        self.actor = self.poly = self.view = None
        self.last_context = None

    def cleanup(self):
        self.deactivate()
        self.timer.disconnect("timeout()", self.refresh)
        self.message_callback = self.threshold_callback = None

    def clear(self):
        self.timer.stop()
        self.xy = None
        self.candidate = self.candidate_key = None
        if self.actor is not None:
            self.actor.VisibilityOff()
            self.view.sliceView().scheduleRender()

    def message(self, text):
        if self.message_callback:
            self.message_callback(text)

    def set_tolerance(self, value):
        self.tolerance = max(1,min(100,int(value)))
        slicer.app.settings().setValue("BreastContour/SmartTolerance", self.tolerance)
        self.candidate_key = None
        if self.threshold_callback:
            self.threshold_callback(self.tolerance)
        if self.xy is not None and self.active:
            self.timer.start()

    def context(self, view):
        node = self.scriptedEffect.parameterSetNode()
        if node is None:
            return None
        volume = node.GetSourceVolumeNode()
        segmentation = node.GetSegmentationNode()
        sid = node.GetSelectedSegmentID()
        if volume is None or segmentation is None or not sid:
            return None
        if volume.GetParentTransformNode() or segmentation.GetParentTransformNode():
            return None
        inverse = vtk.vtkMatrix4x4()
        volume.GetRASToIJKMatrix(inverse)
        slice_matrix = view.mrmlSliceNode().GetSliceToRAS()
        ijk = inverse.MultiplyPoint(tuple(slice_matrix.GetElement(i,3) for i in range(3))+(1,))
        frame = int(round(ijk[2]))
        image = volume.GetImageData()
        if image is None or not (0 <= frame < image.GetDimensions()[2]):
            return None
        return (volume.GetID(), image.GetMTime(), frame, segmentation.GetID(), str(sid))

    def on_parameter_changed(self, caller=None, event=None):
        self.updateGUIFromMRML()

    def updateGUIFromMRML(self):
        if self.active and self.view is not None:
            context = self.context(self.view)
            if context != self.last_context:
                self.clear()
                self.last_context = context

    def processViewNodeEvents(self, callerViewNode, eventId, viewWidget):
        if self.active and self.view == viewWidget:
            context = self.context(viewWidget)
            if context != self.last_context:
                self.clear()
                self.last_context = context
            elif self.xy is not None:
                # Zoom/pan changes preview projection but not candidate pixels.
                self.timer.start()

    def processInteractionEvents(self, interactor, eventId, viewWidget):
        if not self.active or viewWidget.className() != "qMRMLSliceWidget":
            return False
        if eventId == vtk.vtkCommand.LeaveEvent:
            self.clear()
            return False
        if eventId == vtk.vtkCommand.KeyPressEvent:
            if interactor.GetKeySym() == "Escape":
                self.clear()
                return True
            return False
        if eventId in (vtk.vtkCommand.MouseWheelForwardEvent, vtk.vtkCommand.MouseWheelBackwardEvent):
            self.view = viewWidget
            self.xy = tuple(interactor.GetEventPosition())
            self.set_tolerance(self.tolerance + (1 if eventId == vtk.vtkCommand.MouseWheelForwardEvent else -1))
            return True
        if eventId == vtk.vtkCommand.MouseMoveEvent:
            self.view = viewWidget
            self.xy = tuple(interactor.GetEventPosition())
            # Fixed-rate coalescing rather than debouncing indefinitely while moving.
            if not self.timer.isActive():
                self.timer.start()
            return True
        if eventId == vtk.vtkCommand.LeftButtonPressEvent:
            if interactor.GetControlKey() or interactor.GetShiftKey() or interactor.GetAltKey():
                return False
            self.view = viewWidget
            self.xy = tuple(interactor.GetEventPosition())
            self.timer.stop()
            self.refresh()
            self.confirm()
            return True
        if eventId == vtk.vtkCommand.LeftButtonReleaseEvent:
            return True
        return False

    def refresh(self):
        import time
        started = time.perf_counter()
        if not self.active or self.xy is None or self.view is None:
            return
        context = self.context(self.view)
        if context is None:
            self.clear()
            self.message("请先选择有效的图像和工作分段")
            return
        self.last_context = context
        node = self.scriptedEffect.parameterSetNode()
        volume = node.GetSourceVolumeNode()
        matrix = vtk.vtkMatrix4x4()
        volume.GetRASToIJKMatrix(matrix)
        xy_ras = self.view.mrmlSliceNode().GetXYToRAS().MultiplyPoint((self.xy[0],self.xy[1],0,1))
        ijk = matrix.MultiplyPoint(xy_ras)
        frame = context[2]
        array = slicer.util.arrayFromVolume(volume)  # zero-copy view, no volume export
        if not (0 <= ijk[0] < array.shape[2] and 0 <= ijk[1] < array.shape[1]) or abs(ijk[2]-frame)>0.01:
            self.clear()
            self.message("鼠标不在原始图像平面内")
            return
        seed = (min(array.shape[1]-1,int(round(ijk[1]))),min(array.shape[2]-1,int(round(ijk[0]))))
        cache_key = context[:3]
        if cache_key != self.cache_key:
            self.prepared = prepare_frame(array[frame])
            self.cache_key = cache_key
        key = (context,seed,self.tolerance)
        if key != self.candidate_key:
            self.candidate = candidate_from_prepared(self.prepared,seed,self.tolerance)
            self.candidate_key = key
        # Keep intermediate/out-of-range candidates visible even when confirmation
        # is disabled, so the operator can see the extent and adjust tolerance.
        self.display(self.candidate.contour if self.candidate.contour.any() else None, volume, frame)
        action = "左键追加轮廓" if self.candidate.valid else "仅预览，不能确认"
        message = f"边界容忍度 {self.tolerance} · {action}"
        if self.candidate.reason:
            message += " · " + self.candidate.reason
        self.message(message)
        self.last_preview_seconds = time.perf_counter()-started

    def display(self, contour, volume, frame):
        if self.actor is None:
            self.poly = vtk.vtkPolyData()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(self.poly)
            self.actor = vtk.vtkActor2D()
            self.actor.SetMapper(mapper)
            self.actor.GetProperty().SetColor(1,1,0)
            self.actor.GetProperty().SetLineWidth(1.5)
            self.scriptedEffect.addActor2D(self.view,self.actor)
        if contour is None:
            self.actor.VisibilityOff()
            self.view.sliceView().scheduleRender()
            return
        y,x = np.nonzero(contour)
        # Draw the actual contour pixels. Joining every diagonal neighbor by
        # lines creates tiny triangles/extra apparent holes in the preview.
        corners = np.array([[-0.5,-0.5],[0.5,-0.5],[0.5,0.5],[-0.5,0.5]])
        pixels = np.column_stack((x,y))[:,None,:] + corners[None,:,:]
        coordinates = np.column_stack((pixels.reshape(-1,2),
                                       np.full(len(x)*4,frame),np.ones(len(x)*4)))
        ijk_ras = vtk.vtkMatrix4x4()
        volume.GetIJKToRASMatrix(ijk_ras)
        ras_xy = vtk.vtkMatrix4x4()
        ras_xy.DeepCopy(self.view.mrmlSliceNode().GetXYToRAS())
        ras_xy.Invert()
        transform = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Multiply4x4(ras_xy,ijk_ras,transform)
        matrix = np.array([[transform.GetElement(i,j) for j in range(4)] for i in range(4)])
        xy = (coordinates @ matrix.T)[:,:3]
        xy[:,2] = 0
        points = vtk.vtkPoints()
        points.SetData(numpy_to_vtk(xy,deep=True))
        cells = vtk.vtkCellArray()
        for i in range(len(x)):
            cells.InsertNextCell(4)
            for corner in range(4):
                cells.InsertCellPoint(4*i+corner)
        self.poly.SetPoints(points)
        self.poly.SetPolys(cells)
        self.poly.Modified()
        self.actor.VisibilityOn()
        self.view.sliceView().scheduleRender()

    def confirm(self):
        if self.candidate is None or not self.candidate.valid:
            return False
        context = self.context(self.view)
        if context != self.candidate_key[0]:
            self.clear()
            return False
        node = self.scriptedEffect.parameterSetNode()
        volume = node.GetSourceVolumeNode()
        segmentation = node.GetSegmentationNode()
        sid = str(node.GetSelectedSegmentID())
        frame = context[2]
        y,x = np.nonzero(self.candidate.contour)
        segment = segmentation.GetSegmentation().GetSegment(sid)
        image = segment.GetRepresentation("Binary labelmap")
        # Compare only candidate coordinates in the native cropped representation.
        if image is not None and image.GetNumberOfPoints():
            x0,x1,y0,y1,z0,z1 = image.GetExtent()
            if z0 <= frame <= z1 and x.min()>=x0 and x.max()<=x1 and y.min()>=y0 and y.max()<=y1:
                native = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(image.GetDimensions()[::-1])
                if np.all(native[frame-z0,y-y0,x-x0] == segment.GetLabelValue()):
                    self.message("当前轮廓已存在，无需重复确认")
                    return False
        if not self.scriptedEffect.confirmCurrentSegmentVisible():
            return False
        # Compact modifier in the original IJK coordinates: one slice only.
        x0,x1,y0,y1 = int(x.min()),int(x.max()),int(y.min()),int(y.max())
        crop = np.ascontiguousarray(self.candidate.contour[y0:y1+1,x0:x1+1],dtype=np.uint8)
        modifier = slicer.vtkOrientedImageData()
        modifier.SetExtent(x0,x1,y0,y1,frame,frame)
        matrix = vtk.vtkMatrix4x4()
        volume.GetIJKToRASMatrix(matrix)
        modifier.SetGeometryFromImageToWorldMatrix(matrix)
        modifier.GetPointData().SetScalars(numpy_to_vtk(crop.ravel(),deep=True,array_type=vtk.VTK_UNSIGNED_CHAR))
        self.scriptedEffect.saveStateForUndo()
        self.scriptedEffect.modifySelectedSegmentByLabelmap(modifier,slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeAdd)
        self.message("已追加轮廓 · Ctrl+Z 撤销")
        return True
