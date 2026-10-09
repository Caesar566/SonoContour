set(MODULE_NAME BreastContour)
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  BreastContourLib/__init__.py
  BreastContourLib/core.py
  BreastContourLib/runtime.py
)
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
)
