import importlib
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image, ImageCms

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class EngineTests(unittest.TestCase):
    def setUp(self):
        try: self.e = importlib.import_module('retouch.engine')
        except ModuleNotFoundError: self.fail('Missing non-generative renderer')
        self.root = Path(tempfile.mkdtemp(prefix='retouch-engine-test-')).resolve()
        self.array = np.random.default_rng(61).integers(10, 240, (64, 96, 3), dtype=np.uint8)
        self.image = Image.fromarray(self.array)

    def test_zero_parameters_are_exactly_identity(self):
        result, report = self.e.render(self.image, {}, None, None)
        self.assertTrue(np.array_equal(np.asarray(result), self.array))

    def test_explicit_skin_mask_keeps_continuous_coverage_independent_of_color(self):
        skin=Image.new('L',self.image.size,255);skin.paste(0,(20,20,40,30))
        protect=Image.new('L',self.image.size,0)
        actual,_,coverage=self.e.make_masks(self.image,{},skin_override=skin,protection_override=protect)
        self.assertTrue(np.array_equal(np.asarray(actual),np.asarray(skin)))
        self.assertTrue(coverage['needs_visual_review'])
        with self.assertRaises(ValueError):self.e.make_masks(self.image,{},skin_override=Image.new('L',(8,8)))
        with self.assertRaises(ValueError):self.e.make_masks(self.image,{},skin_override=skin.convert('RGB'))

    def test_smoothing_strength_reduces_texture_progressively_and_preserves_exclusions(self):
        try:import cv2
        except ImportError:self.skipTest('Optional OpenCV unavailable')
        rng=np.random.default_rng(17)
        array=np.clip(np.array([175,140,115])+rng.normal(0,10,(180,220,3)),0,255).astype(np.uint8)
        image=Image.fromarray(array);skin=Image.new('L',image.size,255)
        skin.paste(0,(65,60,155,100));protection=Image.new('L',image.size,0);protection.paste(255,(0,0,25,180))
        effects=[]
        for strength in (.25,.5,1.):
            result,report=self.e.render(image,{'skin_smooth':strength},skin,protection)
            out=np.asarray(result);effects.append(np.abs(out.astype(float)-array)[np.asarray(skin)>0].mean())
            self.assertTrue(np.array_equal(out[60:100,65:155],array[60:100,65:155]),'feature exclusions must stay exact at every strength')
            self.assertTrue(np.array_equal(out[:,:25],array[:,:25]),'protection must stay exact at every strength')
            self.assertTrue(report['skin_smoothing_regions'])
        self.assertGreater(effects[1],effects[0]*1.3)
        self.assertGreater(effects[2],effects[1]*1.3)

    def test_skin_smoothing_regions_do_not_create_horizontal_tile_seams(self):
        try:import cv2
        except ImportError:self.skipTest('Optional OpenCV unavailable')
        rng=np.random.default_rng(18)
        array=np.clip(150+rng.normal(0,8,(520,80,3)),0,255).astype(np.uint8)
        image=Image.fromarray(array);skin=Image.new('L',image.size,255)
        actual,report=self.e.render(image,{'skin_smooth':1.},skin,None)
        settings=report['skin_smoothing_regions'][0]
        expected=cv2.bilateralFilter(array,settings['diameter_px'],settings['sigma_color_dn'],settings['sigma_space_px'])
        self.assertTrue(np.array_equal(np.asarray(actual),expected),'whole connected region must match the same filter across former 256-row tile boundaries')

    def test_protected_core_is_unchanged_after_all_modules(self):
        protection = Image.new('L', self.image.size, 0)
        protection.paste(255, (0, 0, 35, 64))
        skin = Image.new('L', self.image.size, 255)
        result, report = self.e.render(self.image, {'exposure_ev': .4, 'shadows': .2,
            'skin_ev': .1, 'skin_db': 2., 'skin_smooth': .15, 'sharpen': .15}, skin, protection)
        self.assertTrue(np.array_equal(np.asarray(result)[:, :35], self.array[:, :35]))
        self.assertFalse(np.array_equal(np.asarray(result)[:, 35:], self.array[:, 35:]))
        self.assertTrue(report['protected_core_exact'])

    def test_bad_mask_size_and_nonfinite_parameter_are_rejected(self):
        with self.assertRaises(ValueError): self.e.render(self.image, {}, Image.new('L', (8, 8)), None)
        with self.assertRaises(ValueError): self.e.render(self.image, {'exposure_ev': float('nan')}, None, None)
        with self.assertRaises(ValueError): self.e.render(self.image, {'exposure_ev': 40}, None, None)

    def test_subject_statistics_do_not_follow_bright_background(self):
        image = Image.new('RGB', (100, 100), (245, 245, 245))
        image.paste((45, 40, 35), (70, 60, 90, 90))
        report = self.e.analyze(image, {'faces': [[.7, .6, .9, .9]]})
        self.assertGreater(report['whole']['p50'], 240)
        self.assertLess(report['subjects'][0]['p50'], 50)
        self.assertGreater(self.e.plan_recipe('natural', report)['exposure_ev'], 0)

    def test_face_color_is_measured_when_background_is_unchanged(self):
        a=Image.new('RGB',(300,300),(100,100,100));b=a.copy()
        a.paste((160,120,90),(120,120,180,180));b.paste((50,135,180),(120,120,180,180))
        first=self.e.analyze(a,{'faces':[[.4,.4,.6,.6]]});second=self.e.analyze(b,{'faces':[[.4,.4,.6,.6]]})
        self.assertGreater(np.linalg.norm(np.array(first['face_colors'])-np.array(second['face_colors'])),20)

    def test_orientation_and_export_profile_are_preserved(self):
        im = Image.new('RGB', (20, 12), (120, 90, 65))
        exif = Image.Exif(); exif[274] = 6
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        source = self.root / 'source.png'; im.save(source, exif=exif, icc_profile=icc)
        canonical, meta = self.e.load_canonical(source)
        self.assertEqual(canonical.size, (12, 20))
        self.assertEqual(meta['source_orientation'], 6)
        target = self.root / 'output.jpg'
        self.e.export_image(canonical, target, meta)
        with Image.open(target) as out:
            self.assertEqual(out.size, canonical.size)
            self.assertEqual(out.getexif().get(274, 1), 1)
            self.assertTrue(out.info.get('icc_profile'))
        with self.assertRaises(ValueError): self.e.export_image(canonical, target, meta)

    def test_unknown_color_space_requires_explicit_assumption(self):
        source = self.root / 'source.png'; self.image.save(source)
        with self.assertRaises(ValueError): self.e.load_canonical(source)
        image, meta = self.e.load_canonical(source, assume_srgb=True)
        self.assertIn('assumed', meta['input_color'])

    def test_lab_roundtrip_has_at_most_one_dn_error(self):
        rgb = self.array.astype(np.float32) / 255
        out = self.e.lab_to_rgb(self.e.rgb_to_lab(rgb))
        self.assertLessEqual(np.abs(np.rint(out * 255) - self.array).max(), 1)

    def test_blue_reflection_adjustment_preserves_other_person(self):
        im=Image.new('RGB',(100,60),(90,100,155));im.paste((160,125,90),(50,0,100,60))
        balance=self.e.balance_recipe(im,{'faces':[[0,0,.5,1],[.5,0,1,1]]})
        self.assertGreater(balance[0]['db'],0)
        self.assertEqual(balance[1]['db'],0)
        result,_=self.e.render(im,{'skin_balance':balance},Image.new('L',im.size,255),None)
        self.assertFalse(np.array_equal(np.asarray(result)[:,:50],np.asarray(im)[:,:50]))
        self.assertLessEqual(np.abs(np.asarray(result)[:,50:].astype(int)-np.asarray(im)[:,50:]).max(),1)

    def test_blue_clothes_outside_face_are_not_skin(self):
        image=Image.new('RGB',(200,200),(80,90,140));image.paste((90,100,155),(70,20,130,80))
        skin,_,_=self.e.make_masks(image,{'faces':[[.35,.1,.65,.4]]},Image.new('L',image.size,255))
        self.assertGreater(skin.getpixel((100,50)),100)
        self.assertLess(skin.getpixel((100,175)),20)

    def test_skin_colored_clothes_are_not_inferred_from_person_mask(self):
        image=Image.new('RGB',(200,200),(160,120,90))
        skin,_,_=self.e.make_masks(image,{'faces':[[.35,.1,.65,.4]]},Image.new('L',image.size,255))
        self.assertGreater(skin.getpixel((100,50)),100)
        self.assertLess(skin.getpixel((100,175)),20)


if __name__ == '__main__': unittest.main()
