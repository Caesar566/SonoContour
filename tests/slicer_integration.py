"""Run with Slicer --testing --no-main-window --python-script this_file.py.

All output targets are temporary copies. The source data are read-only.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback

import numpy as np
import slicer
import vtk
from vtk.util.numpy_support import vtk_to_numpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"BreastContour"))
from BreastContourLib.core import Case, discover_cases
from BreastContourLib.runtime import CaseSession, named_segments


def run():
    root = Path(os.environ.get("BREAST_CONTOUR_DATA", str(Path(__file__).resolve().parents[1])))
    def digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    originals = {p: digest(p) for p in list(root.glob("*/*.nrrd")) + list(root.glob("*/*.dcm")) + [root/'segmentations.xlsx']}
    reports = []
    for index, source in enumerate(discover_cases(root)):
        print("BEGIN", source.uid, flush=True)
        with tempfile.TemporaryDirectory(prefix="breast-contour-test-") as d:
            folder = Path(d)
            (folder/source.image.name).symlink_to(source.image)
            (folder/"Segmentation.nrrd").symlink_to(source.doctor)
            shutil.copy2(source.output, folder/"Segment.nrrd")
            case = Case(source.uid, folder, source.note, source.ranges)
            session = CaseSession()
            try:
                session.load(case)
                print('LOADED', source.uid, flush=True)
                assert session.shape == (500,400,1780)
                assert len(session.doctor_frames) > 0
                assert session.doctor is not session.work
                assert session.editor_node.GetMaskMode() == slicer.vtkMRMLSegmentationNode.EditAllowedEverywhere
                original = slicer.util.loadSegmentation(str(source.output))
                from BreastContourLib.runtime import remove_node
                try:
                    assert set(named_segments(session.work)) == set(named_segments(original))
                    for n in named_segments(original):
                        np.testing.assert_array_equal(session.array(n), session.array(n, original))
                finally:
                    remove_node(original)
                name = next(iter(named_segments(session.work)))
                sid = named_segments(session.work)[name]
                frame = int(session.doctor_frames[0])
                pixels = session.array(name)
                pixels[frame, 5, 5] = 1
                slicer.util.updateSegmentBinaryLabelmapFromArray(pixels, session.work, sid, session.volume)
                del pixels
                assert not session.enforce()
                assert session.array(name)[frame,5,5] == 1
                # Paint notification must never export full volumes or check pixels.
                array_method = session.array
                session.array = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("interactive array export"))
                for _ in range(1000):
                    session._on_modified()
                slicer.app.processEvents()
                session.array = array_method
                print('EDITED', source.uid, flush=True)
                assert session.dirty
                issues = session.check()
                print('CHECKED', source.uid, len(issues), flush=True)
                assert any(i.frame == frame and i.kind == "疑似未闭合" for i in issues)
                backup = session.save()
                print('SAVED', source.uid, flush=True)
                assert backup and backup.exists()
                assert not session.dirty
                assert not list(folder.glob(".Segment-*"))
                reopened = slicer.util.loadSegmentation(str(case.output))
                try:
                    assert set(named_segments(reopened, True)) == set(named_segments(session.work))
                    assert session.array(name, reopened)[frame,5,5] == 1
                    for n in named_segments(reopened):
                        np.testing.assert_array_equal(session.array(n, reopened), session.array(n))
                finally:
                    remove_node(reopened)
                reports.append({"case":source.uid,"doctor_reference_frames":len(session.doctor_frames),
                                "issues":len(issues),"roundtrip":"passed","doctor_frame_edit":"passed",
                                "existing_pixels_unchanged_on_load":True,"interactive_no_array_export":True})
                print("PASS", source.uid, "reference",len(session.doctor_frames), "issues",len(issues), flush=True)
            finally:
                session.close()
                slicer.mrmlScene.Clear(0)
    source = discover_cases(root)[2]
    with tempfile.TemporaryDirectory(prefix="breast-contour-new-") as d:
        folder = Path(d)
        (folder/source.image.name).symlink_to(source.image)
        (folder/"Segmentation.nrrd").symlink_to(source.doctor)
        session = CaseSession()
        try:
            session.load(Case(source.uid, folder, source.note, source.ranges))
            assert len(named_segments(session.work)) == len(named_segments(session.doctor))
            assert all(not session.array(n).any() for n in named_segments(session.work))
            assert (folder/"Segment.nrrd").exists()
            assert not session.dirty
            assert session.save().exists()
            reports.append({"new_file_empty_roundtrip":"passed","doctor_pixels_not_copied":True})
        finally:
            session.close()
    for path, digest in originals.items():
        with path.open('rb') as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == digest, "source data changed"
    report_path = Path(os.environ.get("BREAST_CONTOUR_REPORT", "/tmp/ohslicer-integration-report.json"))
    report_path.write_text(json.dumps({"slicer":slicer.app.applicationVersion,"cases":reports,"source_unchanged":True},ensure_ascii=False,indent=2))
    print("ALL INTEGRATION TESTS PASSED", report_path, flush=True)


try:
    run()
except Exception:
    traceback.print_exc()
    slicer.util.exit(1)
else:
    slicer.util.exit(0)
