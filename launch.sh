#!/bin/sh
set -eu
task_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${BREAST_CONTOUR_SLICER:-Slicer}" \
    --additional-module-path "$task_script_dir/BreastContour" \
    --python-code "slicer.util.selectModule('BreastContour')" "$@"
