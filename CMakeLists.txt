cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)
project(BreastContour)

find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

set(EXTENSION_HOMEPAGE "https://www.slicer.org/")
set(EXTENSION_CATEGORY "Segmentation")
set(EXTENSION_CONTRIBUTORS "BreastContour")
set(EXTENSION_DESCRIPTION "Breast ultrasound pixel-contour annotation with protected physician frames")
set(EXTENSION_DEPENDS "NA")
add_subdirectory(BreastContour)
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
