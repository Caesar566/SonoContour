"""Cold widget startup: do not preload the effect or its algorithm package."""
import traceback
import slicer


try:
    widget = slicer.modules.breastcontour.widgetRepresentation().self()
    assert hasattr(widget, "case_combo"), "Case controls did not initialize"
    assert hasattr(widget, "smart_tolerance"), "Smart contour controls missing"
    assert hasattr(widget, "editor"), "Segment editor missing"
    assert slicer.modules._breastContourSmartRegistered
    # Effect loading must leave the library package available for later imports.
    from BreastContourLib.smart_contour import compute_candidate
    assert callable(compute_candidate)
    effect = widget.editor.effectByName("SmartContour")
    assert effect is not None and effect.self() is not None
    print("COLD STARTUP PASSED")
except Exception:
    traceback.print_exc()
    slicer.util.exit(1)
else:
    slicer.util.exit(0)
