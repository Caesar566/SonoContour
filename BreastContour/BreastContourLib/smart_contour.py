"""Regional low-echo candidates stopped by intensity edges, independent of Slicer."""
from dataclasses import dataclass
import numpy as np
from scipy import ndimage as ndi
from scipy.spatial import ConvexHull, QhullError


@dataclass
class Candidate:
    region: np.ndarray
    contour: np.ndarray
    valid: bool
    reason: str = ""


def complete_region(region):
    """Add missing connections without dropping lobes; return a single-hole rim."""
    connectivity = ndi.generate_binary_structure(2, 1)
    region = np.asarray(region, dtype=bool).copy()
    def outline(mask):
        contour = mask & ~ndi.binary_erosion(mask, structure=connectivity)
        holes = ndi.binary_fill_holes(contour, structure=connectivity) & ~contour
        single = (ndi.label(holes, connectivity)[1] == 1 and
                  ndi.label(contour, np.ones((3,3), dtype=np.uint8))[1] == 1)
        return contour, single
    contour, single = outline(region)
    if single:
        return region, contour, False, True
    # Widen thin connections locally instead of retaining only the largest lobe.
    for _ in range(3):
        region = ndi.binary_fill_holes(ndi.binary_dilation(region, structure=connectivity),
                                       structure=connectivity)
        contour, single = outline(region)
        if single:
            return region, contour, True, True
    # Last resort: enclose the intermediate region with a convex envelope.
    # Scan-line rasterization includes existing pixels and clips at the image edge.
    y, x = np.nonzero(region & ~ndi.binary_erosion(region, structure=connectivity))
    try:
        points = np.column_stack((x,y))
        vertices = points[ConvexHull(points).vertices]
        other = np.roll(vertices, -1, axis=0)
        for row in range(int(y.min()), int(y.max())+1):
            crosses = (np.minimum(vertices[:,1],other[:,1]) <= row) & (np.maximum(vertices[:,1],other[:,1]) >= row)
            nonhorizontal = crosses & (vertices[:,1] != other[:,1])
            start, end = vertices[nonhorizontal], other[nonhorizontal]
            intersections = start[:,0]+(row-start[:,1])*(end[:,0]-start[:,0])/(end[:,1]-start[:,1])
            horizontal = vertices[crosses & (vertices[:,1] == other[:,1]),0]
            values = np.concatenate((intersections,horizontal))
            if values.size:
                left = max(0,int(np.floor(values.min())))
                right = min(region.shape[1]-1,int(np.ceil(values.max())))
                region[row,left:right+1] = True
        region = ndi.binary_fill_holes(region, structure=connectivity)
        contour, single = outline(region)
    except (ValueError, QhullError):
        pass
    return region, contour, True, single


def prepare_frame(frame):
    frame = np.asarray(frame)
    if frame.ndim != 2 or not np.isfinite(frame).all():
        raise ValueError("需要有效的二维灰度图像")
    low, high = np.percentile(frame, (1, 99))
    if high <= low:
        return None
    normalized = np.clip((frame.astype(np.float32) - low) * (255.0 / (high-low)), 0, 255)
    return ndi.gaussian_filter(normalized, sigma=1)


def candidate_from_prepared(prepared, seed, tolerance=15, reference_radius=6):
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
    radius = min(50, max(0, int(reference_radius)))
    y0, y1, x0, x1 = max(0,y-radius), min(h,y+radius+1), max(0,x-radius), min(w,x+radius+1)
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
    # Padding preserves observed pixels at the edge during gap completion.
    padded = ndi.binary_closing(np.pad(region, 2), structure=connectivity, iterations=1)
    region |= padded[2:-2,2:-2]
    region = ndi.binary_fill_holes(region, structure=connectivity)
    count = int(region.sum())
    if count < 9:
        region, contour, _, _ = complete_region(region)
        return Candidate(region, contour, False, "候选小于 9 像素，仅预览；请调整位置或容忍度")
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
    region, contour, repaired, single = complete_region(region)
    warnings = []
    if repaired:
        warnings.append("已自动补全连接，请复核补全部分")
    if touches(region):
        warnings.append("候选到达图像边缘，沿边缘闭合；图像外范围未知")
    if int(region.sum()) > h*w*0.30:
        warnings.append("候选超过图像面积的 30%，仅预览；请减小容忍度")
        return Candidate(region, contour, False, "；".join(warnings))
    if not single:
        warnings.append("未形成单一内部空洞，仅预览；请调整位置或容忍度")
    return Candidate(region, contour, single, "；".join(warnings))


def compute_candidate(frame, seed, tolerance=15, reference_radius=6):
    return candidate_from_prepared(prepare_frame(frame), seed, tolerance, reference_radius)
