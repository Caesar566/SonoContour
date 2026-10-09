"""Small Slicer tests for geometry rejection and ambiguous segment names."""
from pathlib import Path
import sys
import traceback
import numpy as np
import slicer
import vtk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"BreastContour"))
from BreastContourLib.runtime import CaseSession, named_segments, remove_node


def expect_error(function, text):
    try:
        function()
    except ValueError as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError('expected geometry/name rejection')


def run():
    volume = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLScalarVolumeNode')
    slicer.util.updateVolumeFromArray(volume, np.zeros((5,12,10),dtype=np.uint8))
    volume.SetSpacing(0.1,0.1,1)
    segmentation = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLSegmentationNode')
    segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
    sid = segmentation.GetSegmentation().AddEmptySegment('lesion', 'lesion')
    pixels = np.zeros((5,12,10),dtype=np.uint8)
    pixels[2,4,4] = 1
    slicer.util.updateSegmentBinaryLabelmapFromArray(pixels,segmentation,sid,volume)
    session = CaseSession()
    session.volume, session.shape = volume, pixels.shape
    session.work = segmentation
    transform = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLLinearTransformNode')
    segmentation.SetAndObserveTransformNodeID(transform.GetID())
    expect_error(session.enforce, '空间变换')
    segmentation.SetAndObserveTransformNodeID(None)
    remove_node(transform)
    session.validate_geometry(segmentation)
    image = segmentation.GetSegmentation().GetSegment(sid).GetRepresentation('Binary labelmap')
    matrix = vtk.vtkMatrix4x4()
    image.GetImageToWorldMatrix(matrix)
    original = vtk.vtkMatrix4x4()
    original.DeepCopy(matrix)
    matrix.SetElement(0,3,10)
    image.SetGeometryFromImageToWorldMatrix(matrix)
    expect_error(lambda: session.validate_geometry(segmentation),'几何')
    image.SetGeometryFromImageToWorldMatrix(original)
    other = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLScalarVolumeNode')
    slicer.util.updateVolumeFromArray(other, np.zeros((6,12,10),dtype=np.uint8))
    other.SetSpacing(0.1,0.1,1)
    segmentation.SetReferenceImageGeometryParameterFromVolumeNode(other)
    expect_error(lambda: session.validate_geometry(segmentation),'尺寸')
    segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
    segmentation.GetSegmentation().AddEmptySegment('ambiguous','lesion')
    expect_error(lambda: named_segments(segmentation),'名称重复')
    remove_node(segmentation)
    remove_node(volume)
    remove_node(other)
    Path('/tmp/ohslicer-geometry-report.txt').write_text('matching geometry: passed\ngeometry mismatch: rejected\nsize mismatch: rejected\nspatial transform: rejected\nduplicate segment names: rejected\n')
    print('ALL GEOMETRY TESTS PASSED',flush=True)


try:
    run()
except Exception:
    traceback.print_exc()
    slicer.util.exit(1)
else:
    slicer.util.exit(0)
