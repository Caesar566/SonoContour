from pathlib import Path
import sys
import unittest
import numpy as np
from scipy import ndimage as ndi
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'BreastContour'))
from BreastContourLib.smart_contour import prepare_frame, compute_candidate, candidate_from_prepared, complete_region
from BreastContourLib.core import contour_problems


def scene():
    image = np.full((120,180),170,dtype=np.float32)
    y,x = np.indices(image.shape)
    mask = ((y-60)/22)**2+((x-65)/30)**2 <= 1
    image[mask] = 60
    image[5:10,5:10] = 230
    return image


class SmartTests(unittest.TestCase):
    def test_reference_radius_switches_between_point_and_neighborhood(self):
        image = scene()
        image[59:62,64:67] = 170
        prepared = prepare_frame(image)
        point = candidate_from_prepared(prepared,(60,65),15,reference_radius=0)
        regional = candidate_from_prepared(prepared,(60,65),15,reference_radius=6)
        self.assert_single_hole(regional)
        self.assertGreater(regional.region.sum(),1500)
        self.assertFalse(np.array_equal(point.region,regional.region))

    def test_reference_radius_is_clipped_at_image_edge(self):
        image = scene()
        for radius in (0,1,6,50):
            c = compute_candidate(image,(0,0),reference_radius=radius)
            self.assertEqual(c.region.shape,image.shape)

    def test_connected_black_block_overrides_strong_gradient(self):
        image = np.full((120,180), 220.0)
        image[35:85,35:125] = 100
        # This block is much darker, with a gradient above wheel tolerance.
        # Its extent exceeds the four-pixel boundary refinement band.
        image[35:85,85:125] = 0
        c = candidate_from_prepared(image, (60,55), 5)
        self.assert_single_hole(c)
        interior = ndi.binary_fill_holes(c.contour) & ~c.contour
        self.assertTrue(np.all(interior[35:85,35:125]))
        self.assertFalse(c.region[60,140])

    def test_smoothed_connected_black_block_is_not_discarded(self):
        image = np.full((120,180), 220.0)
        image[35:85,35:125] = 100
        image[35:85,85:125] = 0
        c = compute_candidate(image, (60,55), 5)
        self.assert_single_hole(c)
        interior = ndi.binary_fill_holes(c.contour) & ~c.contour
        black = image == 0
        self.assertGreater(np.count_nonzero(interior & black)/black.sum(), 0.98)

    def test_disconnected_blacker_block_stays_separate(self):
        image = np.full((120,180), 220.0)
        image[35:85,30:80] = 100
        image[35:85,110:150] = 0
        c = candidate_from_prepared(image, (60,55), 5)
        self.assert_single_hole(c)
        self.assertTrue(c.region[60,55])
        self.assertFalse(c.region[:,110:150].any())

    def test_outline_on_bright_side_encloses_dark_lesion(self):
        image = scene()
        dark = image < 100
        c = compute_candidate(image, (60,65), 15)
        self.assert_single_hole(c)
        interior = ndi.binary_fill_holes(c.contour) & ~c.contour
        self.assertGreater(np.count_nonzero(interior & dark)/dark.sum(), 0.98)
        self.assertGreater(np.mean(image[c.contour] >= 170), 0.90)
        self.assertLess(c.region.sum(), dark.sum()*1.25)

    def test_sharp_interface_outline_is_outside_dark_pixels(self):
        image = np.full((100,140), 200.0)
        image[30:70,45:95] = 40
        c = candidate_from_prepared(image, (50,65), 15)
        self.assert_single_hole(c)
        self.assertTrue(np.all(image[c.contour] == 200))
        self.assertTrue(np.all((ndi.binary_fill_holes(c.contour) & ~c.contour)[30:70,45:95]))

    def assert_single_hole(self, candidate):
        self.assertTrue(candidate.valid, candidate.reason)
        connectivity = ndi.generate_binary_structure(2, 1)
        holes = ndi.binary_fill_holes(candidate.contour, structure=connectivity) & ~candidate.contour
        self.assertEqual(ndi.label(holes, connectivity)[1], 1)
        self.assertEqual(ndi.label(candidate.contour, np.ones((3,3)))[1], 1)

    def test_bright_pointer_uses_surrounding_dark_region(self):
        image = scene()
        image[59:62,64:67] = 170
        c = compute_candidate(image, (60,65), 15)
        self.assert_single_hole(c)
        self.assertGreater(c.region.sum(), 1500)
        self.assertFalse(c.contour[60,65])

    def test_multiple_internal_bright_islands_leave_one_hole(self):
        image = scene()
        image[50:56,50:58] = 170
        image[64:72,70:78] = 170
        c = compute_candidate(image, (60,65), 15)
        self.assert_single_hole(c)
        self.assertTrue(c.region[52,54])
        self.assertTrue(c.region[67,74])

    def test_thin_bridge_does_not_create_multiple_holes(self):
        prepared = np.full((100,140), 170.0)
        prepared[30:60,25:50] = 40
        prepared[30:60,70:95] = 40
        prepared[44:47,49:71] = 40
        c = candidate_from_prepared(prepared, (45,35), 15)
        self.assert_single_hole(c)

    def test_edge_tolerance_crosses_weak_boundary_before_strong_boundary(self):
        prepared = np.full((100,140), 170.0)
        prepared[25:75,35:95] = 65
        prepared[35:65,45:85] = 40
        low = candidate_from_prepared(prepared, (50,65), 5)
        high = candidate_from_prepared(prepared, (50,65), 20)
        self.assert_single_hole(low)
        self.assert_single_hole(high)
        self.assertGreater(high.region.sum(), low.region.sum())
        self.assertFalse(high.region[20,65])

    def test_clear_lesion_closed_hollow_output(self):
        image=scene(); before=image.copy()
        c=compute_candidate(image,(60,65),15)
        self.assertTrue(c.valid,c.reason)
        self.assertTrue(c.region[60,65])
        self.assertFalse(c.contour[60,65])
        self.assertFalse(any(k=='疑似未闭合' for k,_ in contour_problems(c.contour)))
        np.testing.assert_array_equal(image,before)

    def test_speckle(self):
        image=scene()+np.random.default_rng(42).normal(0,5,(120,180))
        c=compute_candidate(image,(60,65),20)
        self.assertTrue(c.valid,c.reason)
        self.assertGreater(c.region.sum(),1500)

    def test_tolerance_grows_gradient(self):
        y,x=np.indices((120,180)); image=scene()
        region=image<100
        image[region]=50+np.hypot(y[region]-60,x[region]-65)*1.5
        prepared=prepare_frame(image)
        low=candidate_from_prepared(prepared,(60,65),8)
        high=candidate_from_prepared(prepared,(60,65),35)
        self.assertTrue(low.valid,low.reason); self.assertTrue(high.valid,high.reason)
        self.assertGreater(high.region.sum(),low.region.sum())

    def test_nearby_lesion_not_selected(self):
        image=scene();image[45:75,120:145]=60
        c=compute_candidate(image,(60,65),15)
        self.assertTrue(c.valid,c.reason); self.assertFalse(c.region[60,130])

    def test_constant_and_invalid_seed(self):
        self.assertFalse(compute_candidate(np.ones((20,30)),(10,10)).valid)
        self.assertFalse(compute_candidate(scene(),(-1,50)).valid)
        self.assertFalse(compute_candidate(scene(),(60,180)).valid)

    def test_leak_and_border(self):
        leak = compute_candidate(scene(),(20,170),100)
        self.assertFalse(leak.valid)
        self.assertTrue(leak.contour.any())
        image=scene(); image[40:80,0:30]=60
        c=compute_candidate(image,(60,15),15)
        self.assert_single_hole(c); self.assertIn('边缘',c.reason)
        self.assertTrue(c.contour[:,0].any())

    def test_all_image_edges_show_clipped_closed_outline(self):
        for side in ('left','right','top','bottom'):
            image = np.full((100,140),200.0)
            slices = {'left': (slice(30,70),slice(0,30)),
                      'right': (slice(30,70),slice(110,140)),
                      'top': (slice(0,30),slice(50,90)),
                      'bottom': (slice(70,100),slice(50,90))}
            seeds = {'left': (50,10),'right': (50,130),'top': (10,70),'bottom': (90,70)}
            image[slices[side]] = 20
            with self.subTest(side=side):
                c = candidate_from_prepared(image,seeds[side],15)
                self.assert_single_hole(c)
                self.assertTrue(np.all(c.region[slices[side]]))
                self.assertIn('图像外范围未知',c.reason)

    def test_completion_preserves_both_lobes(self):
        region = np.zeros((70,100),bool)
        region[20:45,15:35] = True
        region[20:45,60:80] = True
        region[32,35:60] = True
        completed, contour, repaired, single = complete_region(region)
        self.assertTrue(repaired);self.assertTrue(single)
        self.assertTrue(np.all(completed[region]))
        self.assertEqual(ndi.label(ndi.binary_fill_holes(contour) & ~contour)[1],1)

    def test_convex_completion_for_disconnected_intermediate_parts(self):
        region = np.zeros((70,100),bool)
        region[20:45,15:35] = True
        region[20:45,65:85] = True
        completed, contour, repaired, single = complete_region(region)
        self.assertTrue(repaired);self.assertTrue(single)
        self.assertTrue(np.all(completed[region]))
        self.assertTrue(completed[32,50])
        self.assertEqual(ndi.label(ndi.binary_fill_holes(contour) & ~contour)[1],1)

    def test_small_and_large(self):
        prepared=np.ones((100,100))*100;prepared[50,50]=0
        self.assertFalse(candidate_from_prepared(prepared,(50,50),1).valid)
        prepared[10:90,10:90]=0
        c=candidate_from_prepared(prepared,(50,50),1)
        self.assertFalse(c.valid);self.assertIn('30%',c.reason)
        self.assertTrue(c.contour.any())

    def test_holes_filled_for_candidate_only(self):
        image=scene();image[57:64,62:69]=170
        c=compute_candidate(image,(60,55),15)
        self.assertTrue(c.valid,c.reason);self.assertTrue(c.region[60,65]);self.assertFalse(c.contour[60,65])

if __name__=='__main__':unittest.main(verbosity=2)
