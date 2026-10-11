"""Regional low-echo candidates stopped by intensity edges, independent of Slicer."""
from dataclasses import dataclass
import numpy as np
from scipy import ndimage as ndi


@dataclass
class Candidate:
    region: np.ndarray
    contour: np.ndarray
    valid: bool
    reason: str = ""


def prepare_frame(frame):
    frame = np.asarray(frame)
    if frame.ndim != 2 or not np.isfinite(frame).all():
        raise ValueError("需要有效的二维灰度图像")
    low, high = np.percentile(frame, (1, 99))
    if high <= low:
        return None
    normalized = np.clip((frame.astype(np.float32) - low) * (255.0 / (high-low)), 0, 255)
    return ndi.gaussian_filter(normalized, sigma=1)


def candidate_from_prepared(prepared, seed, tolerance=15):
    if prepared is None:
        empty = np.zeros((0, 0), dtype=bool)
        return Candidate(empty, empty, False, "灰度变化不足，无法生成候选")
    h, w = prepared.shape
    empty = np.zeros((h, w), dtype=bool)
    y, x = seed
    if not (0 <= y < h and 0 <= x < w):
        return Candidate(empty, empty, False, "鼠标不在图像内")
    tolerance = min(100, max(1, int(tolerance)))
    # The pointer chooses a neighborhood, not an intensity reference pixel.
    # Ignore bright speckle and locate the nearest representative dark pixel.
    y0, y1, x0, x1 = max(0,y-6), min(h,y+7), max(0,x-6), min(w,x+7)
    patch = prepared[y0:y1, x0:x1]
    dark_level = float(np.percentile(patch, 40))
    yy, xx = np.nonzero(patch <= dark_level)
    nearest = np.argmin((yy+y0-y)**2 + (xx+x0-x)**2)
    anchor = (int(yy[nearest]+y0), int(xx[nearest]+x0))
    # Sobel / 8 measures the local intensity change per pixel. The wheel
    # controls which edges growth may cross into brighter tissue. Pixels at
    # least as dark as the regional low-echo reference remain eligible even
    # across a strong gradient: internal darker blocks belong to the same
    # connected low-echo candidate. The ceiling prevents bright-tissue drift.
    gradient = np.hypot(ndi.sobel(prepared, axis=0)/8,
                        ndi.sobel(prepared, axis=1)/8)
    ceiling = dark_level + float(np.percentile(patch, 70)-dark_level) + 2*tolerance
    darker_tissue = prepared <= dark_level
    eligible = (prepared <= ceiling) & ((gradient <= tolerance) | darker_tissue)
    connectivity = ndi.generate_binary_structure(2, 1)
    labels, _ = ndi.label(eligible, connectivity)
    label = labels[anchor]
    if not label:
        return Candidate(empty, empty, False, "附近低回声区域边界变化过强，请移动鼠标或增大容忍度")
    region = labels == label
    touches = lambda a: bool(a[0].any() or a[-1].any() or a[:,0].any() or a[:,-1].any())
    # Do not let closing silently trim a leaked component off the frame edge.
    if touches(region):
        return Candidate(region, empty, False, "候选接触图像边缘，请减小容忍度或移动鼠标")
    region = ndi.binary_closing(region, structure=connectivity, iterations=1)
    region = ndi.binary_fill_holes(region, structure=connectivity)
    count = int(region.sum())
    if count < 9:
        return Candidate(region, empty, False, "候选小于 9 像素")
    # Gradient stopping leaves the core on the dark side of a blurred edge.
    # Recover its transition band using local dark/bright contrast, rather
    # than leaving the outline inset in the low-echo area. Limit refinement
    # to four pixels so it cannot jump to a separate nearby dark lesion.
    local_dark = ndi.minimum_filter(prepared, size=9)
    local_bright = ndi.maximum_filter(prepared, size=9)
    contrast = local_bright-local_dark
    bright_side = local_dark + 0.75*contrast
    near_core = ndi.distance_transform_edt(~region) <= 4
    transition = ((prepared <= ceiling) |
                  ((contrast >= 5) & (prepared <= bright_side)))
    region = ndi.binary_propagation(region, structure=connectivity,
                                    mask=region | (near_core & transition))
    region = ndi.binary_fill_holes(region, structure=connectivity)
    # Store the outside pixel rim: dark tissue remains inside, while the
    # contour occupies the adjacent high-echo side of the interface.
    region = ndi.binary_dilation(region, structure=connectivity)
    count = int(region.sum())
    if count > h*w*0.30:
        return Candidate(region, empty, False, "候选超过图像面积的 30%，请减小容忍度")
    if touches(region):
        return Candidate(region, empty, False, "候选接触图像边缘")
    # Thin bridges can divide an outlined region into several enclosed holes.
    # Keep the main interior and rebuild its one-pixel outer boundary.
    interior = ndi.binary_erosion(region, structure=connectivity)
    labels, interiors = ndi.label(interior, connectivity)
    if not interiors:
        return Candidate(region, empty, False, "候选没有有效内部区域")
    if interiors > 1:
        sizes = np.bincount(labels.ravel()); sizes[0] = 0
        interior = labels == int(sizes.argmax())
        region = ndi.binary_fill_holes(ndi.binary_dilation(interior, structure=connectivity),
                                       structure=connectivity)
        if int(region.sum()) < 9:
            return Candidate(region, empty, False, "候选小于 9 像素")
    contour = region & ~ndi.binary_erosion(region, structure=connectivity)
    holes = ndi.binary_fill_holes(contour, structure=connectivity) & ~contour
    if (ndi.label(holes, connectivity)[1] != 1 or
            ndi.label(contour, np.ones((3,3), dtype=np.uint8))[1] != 1):
        return Candidate(region, empty, False, "候选无法形成单一内部空洞，请调整容忍度或位置")
    return Candidate(region, contour, True)


def compute_candidate(frame, seed, tolerance=15):
    return candidate_from_prepared(prepare_frame(frame), seed, tolerance)
