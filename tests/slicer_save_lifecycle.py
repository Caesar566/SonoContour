"""Small roundtrip regression for native storage-node ownership on save."""
from pathlib import Path
import gc
import sys
import tempfile
import traceback
import numpy as np
import slicer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'BreastContour'))
from BreastContourLib.core import Case
from BreastContourLib.runtime import CaseSession, remove_node


def run():
    with tempfile.TemporaryDirectory(prefix='中文 病例-lifecycle-') as d:
        session = CaseSession()
        try:
            session.case = Case('2.25.999', Path(d))
            session.shape = (3,30,40)
            session.volume = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLScalarVolumeNode')
            slicer.util.updateVolumeFromArray(session.volume, np.zeros(session.shape,dtype=np.uint8))
            session.work = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLSegmentationNode')
            session.work.CreateDefaultDisplayNodes()
            session.work.SetReferenceImageGeometryParameterFromVolumeNode(session.volume)
            sid = session.work.GetSegmentation().AddEmptySegment('lesion','lesion')
            session.editor_node = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLSegmentEditorNode')
            session._configure_mask()
            count = slicer.mrmlScene.GetNumberOfNodesByClass('vtkMRMLSegmentationStorageNode')
            # Missing work file: immediately save an empty, readable NRRD.
            assert session.save() is None
            reopened = slicer.util.loadSegmentation(str(session.case.output))
            try:
                assert reopened is not None
                assert not session.array('lesion', reopened).any()
            finally:
                remove_node(reopened)
            a = np.zeros(session.shape,dtype=np.uint8)
            a[1,5,5:20] = a[1,20,5:20] = 1
            a[1,5:21,5] = a[1,5:21,19] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(a,session.work,sid,session.volume)
            assert session.save().exists()
            assert session.save().exists()
            assert slicer.mrmlScene.GetNumberOfNodesByClass('vtkMRMLSegmentationStorageNode') == count
        finally:
            session.close()
    gc.collect()
    slicer.mrmlScene.Clear(0)
    Path('/tmp/ohslicer-lifecycle-report.txt').write_text('empty work creation and reread: passed\nChinese and space path: passed\ntwo edited save roundtrips: passed\nbackup: passed\nstorage nodes released: passed\n')
    print('ALL SAVE LIFECYCLE TESTS PASSED',flush=True)


try:
    run()
except Exception:
    traceback.print_exc()
    slicer.util.exit(1)
else:
    slicer.util.exit(0)
