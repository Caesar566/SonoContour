"""Independent doctor reference and editable contour lifecycle."""
import hashlib

import numpy as np
import slicer
import vtk
import qt
from vtk.util.numpy_support import vtk_to_numpy

from .core import atomic_save, check_arrays, parse_ranges

MASK_NAME = "__BreastContourEditableArea"


def matrix_array(matrix):
    return np.array([[matrix.GetElement(i, j) for j in range(4)] for i in range(4)])


def named_segments(node, include_mask=False):
    result = {}
    segmentation = node.GetSegmentation()
    for index in range(segmentation.GetNumberOfSegments()):
        sid = segmentation.GetNthSegmentID(index)
        name = segmentation.GetSegment(sid).GetName()
        if name == MASK_NAME and not include_mask:
            continue
        if name in result:
            raise ValueError("分段名称重复，无法唯一识别分段：" + name)
        result[name] = sid
    return result


def remove_node(node):
    if node and node.GetScene():
        # Storage/display nodes otherwise accumulate with each case switch.
        storage = node.GetStorageNode() if hasattr(node, "GetStorageNode") else None
        displays = [node.GetNthDisplayNode(i) for i in range(node.GetNumberOfDisplayNodes())] if hasattr(node, "GetNumberOfDisplayNodes") else []
        slicer.mrmlScene.RemoveNode(node)
        if storage and storage.GetScene():
            slicer.mrmlScene.RemoveNode(storage)
        for display in displays:
            if display and display.GetScene():
                slicer.mrmlScene.RemoveNode(display)


class CaseSession:
    def __init__(self, changed=None):
        self.case = self.volume = self.work = self.doctor = self.editor_node = None
        self.shape = None
        self.doctor_frames = np.array([], dtype=int)
        self.dirty = False
        self.changed = changed
        self._observers = []
        self._busy = False
        self.safety_error = None
        self._guard_pending = False

    def close(self):
        for obj, tag in self._observers:
            obj.RemoveObserver(tag)
        self._observers.clear()
        for node in (self.editor_node, self.work, self.doctor, self.volume):
            remove_node(node)
        self.case = self.work = self.doctor = self.volume = self.editor_node = None
        self.doctor_frames = np.array([], dtype=int)
        self.dirty = False
        self._guard_pending = False

    def validate_geometry(self, segmentation):
        reference = vtk.vtkMatrix4x4()
        self.volume.GetIJKToRASMatrix(reference)
        expected = matrix_array(reference)
        # Reference geometry is also checked when present (including empty files).
        parameter = segmentation.GetSegmentation().GetConversionParameter("Reference image geometry")
        if parameter:
            geometry = slicer.vtkOrientedImageData()
            slicer.vtkSegmentationConverter.DeserializeImageGeometry(parameter, geometry, False)
            actual = vtk.vtkMatrix4x4()
            geometry.GetImageToWorldMatrix(actual)
            if not np.allclose(matrix_array(actual), expected, atol=1e-6, rtol=1e-6):
                raise ValueError("分割参考几何与 DCM 不一致，拒绝自动重采样")
            extent = geometry.GetExtent()
            if tuple(extent[2*i+1]-extent[2*i]+1 for i in range(3)) != tuple(self.shape[::-1]):
                raise ValueError("分割参考尺寸与 DCM 不一致")
        for name, sid in named_segments(segmentation).items():
            image = segmentation.GetSegmentation().GetSegment(sid).GetRepresentation("Binary labelmap")
            if image is None or image.GetNumberOfPoints() == 0:
                continue
            actual = vtk.vtkMatrix4x4()
            image.GetImageToWorldMatrix(actual)
            if not np.allclose(matrix_array(actual), expected, atol=1e-6, rtol=1e-6):
                raise ValueError("分段几何与 DCM 不一致：" + name)
            extent = image.GetExtent()
            for axis, dimension in enumerate(self.shape[::-1]):
                if extent[2*axis] < 0 or extent[2*axis+1] >= dimension:
                    raise ValueError("分段范围超出 DCM：" + name)

    def array(self, name, node=None):
        node = node or self.work
        segment = node.GetSegmentation().GetSegment(named_segments(node)[name])
        image = segment.GetRepresentation("Binary labelmap")
        result = np.zeros(self.shape, dtype=np.uint8)
        if image is not None and image.GetNumberOfPoints():
            x0, x1, y0, y1, z0, z1 = image.GetExtent()
            if x0 < 0 or y0 < 0 or z0 < 0 or x1 >= self.shape[2] or y1 >= self.shape[1] or z1 >= self.shape[0]:
                raise ValueError("分段超出参考图像范围：" + name)
            native = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(image.GetDimensions()[::-1])
            result[z0:z1+1, y0:y1+1, x0:x1+1] = native == segment.GetLabelValue()
        return result

    def load(self, case):
        if self.case:
            raise RuntimeError("请先关闭当前病例")
        self.case = case
        self.safety_error = None
        try:
            self.volume = slicer.util.loadVolume(str(case.image), {"name": case.uid + " 图像"})
            if not self.volume or self.volume.GetImageData().GetNumberOfScalarComponents() != 1:
                raise ValueError("第一版仅支持单通道多帧超声 DCM")
            self.shape = slicer.util.arrayFromVolume(self.volume).shape
            if len(self.shape) != 3:
                raise ValueError("DCM 未读取为三维帧堆栈")
            case.ranges, warnings = parse_ranges(case.note, self.shape[0])
            case.warnings = list(dict.fromkeys(case.warnings + warnings))
            self.doctor = doctor = slicer.util.loadSegmentation(str(case.doctor))
            if not doctor:
                raise ValueError("医生标注读取失败")
            self.validate_geometry(doctor)
            doctors = named_segments(doctor)
            if not doctors:
                raise ValueError("医生标注中没有分段")
            present = np.zeros(self.shape[0], dtype=bool)
            for sid in doctors.values():
                segment = doctor.GetSegmentation().GetSegment(sid)
                image = segment.GetRepresentation("Binary labelmap")
                if image is not None and image.GetNumberOfPoints():
                    native = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(image.GetDimensions()[::-1])
                    frames = np.flatnonzero(np.any(native == segment.GetLabelValue(), axis=(1, 2)))
                    present[frames + image.GetExtent()[4]] = True
            self.doctor_frames = np.flatnonzero(present)
            doctor.SetName(case.uid + " 医生参考（不编辑）")
            doctor.SetSaveWithScene(False)
            reference_display = doctor.GetDisplayNode()
            reference_display.SetVisibility2DFill(True)
            reference_display.SetOpacity2DFill(0.35)
            reference_display.SetVisibility2DOutline(False)
            reference_display.SetVisibility3D(False)
            if case.output.exists():
                self.work = slicer.util.loadSegmentation(str(case.output))
                if not self.work:
                    raise ValueError("工作标注读取失败")
                self.validate_geometry(self.work)
            else:
                self.work = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
                self.work.CreateDefaultDisplayNodes()
                # Copy names/colors only. Doctor pixels stay solely in the reference node.
                for sid in doctors.values():
                    source = doctor.GetSegmentation().GetSegment(sid)
                    self.work.GetSegmentation().AddEmptySegment("", source.GetName(), source.GetColor())
            if not named_segments(self.work):
                raise ValueError("工作标注中没有分段")
            self.work.SetName(case.uid + " 我的轮廓")
            self.work.AddDefaultStorageNode()
            self.work.GetStorageNode().SetFileName(str(case.output))
            self.work.SetReferenceImageGeometryParameterFromVolumeNode(self.volume)
            self.editor_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode", "乳腺轮廓编辑参数")
            self._configure_mask()
            if not case.output.exists():
                # Persist the empty work file immediately; no contour check is
                # needed before the first stroke. Use the normal verified writer.
                self.save()
            self.dirty = False
            segmentation = self.work.GetSegmentation()
            for event in (slicer.vtkSegmentation.SegmentModified, slicer.vtkSegmentation.SegmentAdded,
                          slicer.vtkSegmentation.SegmentRemoved, slicer.vtkSegmentation.SourceRepresentationModified):
                self._observers.append((segmentation, segmentation.AddObserver(event, self._on_modified)))
            self._observers.append((self.editor_node, self.editor_node.AddObserver(vtk.vtkCommand.ModifiedEvent, self._on_parameters)))
            display = self.work.GetDisplayNode()
            display.SetVisibility2DFill(True)
            display.SetOpacity2DFill(0.8)
            display.SetVisibility2DOutline(False)
            display.SetVisibility3D(False)
        except Exception:
            self.close()
            raise

    def _configure_mask(self):
        if not self.editor_node:
            return
        old = self.editor_node.StartModify()
        self.editor_node.SetMaskMode(slicer.vtkMRMLSegmentationNode.EditAllowedEverywhere)
        self.editor_node.SetMaskSegmentID("")
        self.editor_node.SetOverwriteMode(slicer.vtkMRMLSegmentEditorNode.OverwriteNone)
        self.editor_node.SetSourceVolumeIntensityMask(False)
        self.editor_node.EndModify(old)

    def _on_parameters(self, caller=None, event=None):
        if self._busy:
            return
        self._busy = True
        try:
            self._configure_mask()
        finally:
            self._busy = False

    def _on_modified(self, caller=None, event=None):
        if self._busy:
            return
        self.dirty = True
        # SegmentModified may be emitted halfway through a native import/undo.
        # Never mutate a segmentation reentrantly inside that transaction.
        if not self._guard_pending:
            self._guard_pending = True
            qt.QTimer.singleShot(0, self._finish_edit)

    def _finish_edit(self):
        self._guard_pending = False
        if self.work and self.changed and self.dirty:
            # Never export or scan labelmaps on the interactive paint/undo path.
            self.changed(False)

    def enforce(self):
        if self.work.GetParentTransformNode() or self.volume.GetParentTransformNode():
            raise ValueError("第一版不支持病例上的空间变换，请重新打开病例")
        self.validate_geometry(self.work)
        return False

    def check(self, progress=None):
        self.enforce()
        issues = []
        occupied = np.zeros(self.shape[0], dtype=bool)
        # Process one full segment at a time to bound memory use.
        for name in named_segments(self.work):
            a = self.array(name)
            occupied |= np.any(a, axis=(1, 2))
            issues.extend(check_arrays({name: a}, [], [], progress))
            del a
        from .core import Issue
        expected = {k for r in self.case.ranges for k in range(r.start, min(self.shape[0], r.end+1))}
        issues.extend(Issue(k, "", "待确认空帧", "表格范围内所有分段均为空", False)
                      for k in sorted(expected) if not occupied[k])
        return sorted(issues, key=lambda x: (x.frame, x.segment, x.kind))

    def save(self):
        self.enforce()
        if self.safety_error:
            raise RuntimeError(self.safety_error)
        names = named_segments(self.work)
        signatures = {}
        for name, sid in names.items():
            segment = self.work.GetSegmentation().GetSegment(sid)
            a = self.array(name)
            signatures[name] = (sid, tuple(segment.GetColor()), hashlib.sha256(a).hexdigest())
            del a
        clone = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "轮廓保存副本")
        clone.SetHideFromEditors(True)
        clone.GetSegmentation().DeepCopy(self.work.GetSegmentation())
        clone.SetReferenceImageGeometryParameterFromVolumeNode(self.volume)
        try:
            def write(path):
                # AddDefaultStorageNode owns/releases the native factory reference.
                # Calling CreateDefaultStorageNode directly leaks that reference.
                clone.AddDefaultStorageNode()
                storage = clone.GetStorageNode()
                if storage is None:
                    raise IOError("无法创建 NRRD 存储节点")
                storage.SetFileName(str(path))
                storage.SetUseCompression(True)
                storage.SetCropToMinimumExtent(False)
                if not storage.WriteData(clone):
                    raise IOError("Slicer 写入 NRRD 失败")

            def validate(path):
                reopened = None
                try:
                    reopened = slicer.util.loadSegmentation(str(path))
                    if reopened is None:
                        raise IOError("无法重新读取临时结果")
                    self.validate_geometry(reopened)
                    loaded = named_segments(reopened, True)
                    if set(loaded) != set(names):
                        raise IOError("保存后分段列表不一致")
                    for name, sid in loaded.items():
                        segment = reopened.GetSegmentation().GetSegment(sid)
                        a = self.array(name, reopened)
                        original_id, color, digest = signatures[name]
                        if sid != original_id or not np.allclose(segment.GetColor(), color):
                            raise IOError("保存后分段属性不一致：" + name)
                        if hashlib.sha256(a).hexdigest() != digest:
                            raise IOError("保存后轮廓像素不一致：" + name)
                        del a
                finally:
                    remove_node(reopened)

            backup = atomic_save(self.case.output, write, validate)
            self.dirty = False
            return backup
        finally:
            remove_node(clone)
