import json
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from retouch import engine
import setup_runtime as runtime

class SkinCandidateTests(unittest.TestCase):
 def test_color_adjustment_survives_zero_texture_mask(self):
  image=Image.new('RGB',(40,40),(120,100,90));skin=Image.new('L',image.size,255);zero=Image.new('L',image.size)
  output,qa=engine.render(image,{'skin_ev':.4,'skin_smooth':1.},skin,zero,zero)
  self.assertFalse(np.array_equal(np.asarray(output),np.asarray(image)))
  self.assertEqual(qa['skin_smoothing_pixels_affected'],0)
 def test_empty_texture_reports_no_skin_changes_for_smoothing_only(self):
  image=Image.new('RGB',(40,40),(120,100,90));skin=Image.new('L',image.size,255);zero=Image.new('L',image.size)
  output,qa=engine.render(image,{'skin_smooth':1.},skin,zero,zero)
  self.assertEqual(qa['skin_pixels_affected'],0);self.assertTrue(np.array_equal(np.asarray(output),np.asarray(image)))
 def test_texture_cannot_expand_skin_or_touch_feature_holes(self):
  rng=np.random.default_rng(14);image=Image.fromarray(rng.integers(70,180,(60,60,3),dtype=np.uint8))
  a=np.zeros((60,60),np.uint8);a[10:50,10:50]=255;a[25:35,25:35]=0
  output,qa=engine.render(image,{'skin_smooth':1.},Image.fromarray(a),Image.new('L',image.size),Image.new('L',image.size,255))
  before=np.asarray(image);after=np.asarray(output)
  self.assertTrue(np.array_equal(before[a==0],after[a==0]))
  self.assertTrue(np.any(before[a>0]!=after[a>0]))
 def test_invalid_texture_size_is_refused(self):
  with self.assertRaises(ValueError):engine.render(Image.new('RGB',(30,30)),{},None,None,Image.new('L',(20,20)))
 def test_uncertain_face_defers_whole_skin_component_without_rectangular_seam(self):
  from retouch.skin_candidates import texture_candidate
  color=np.zeros((60,80),np.float32);color[5:55,5:35]=1;color[5:55,45:75]=1
  texture,n=texture_candidate(color,[[10,15,20,25]])
  self.assertEqual(n,1);self.assertFalse(texture[:,5:35].any());self.assertTrue(np.array_equal(texture[:,45:75],color[:,45:75]))
 def test_auxiliary_model_corruption_is_not_ignored(self):
  root=Path(tempfile.mkdtemp());folder=root/'skill/assets/models';folder.mkdir(parents=True)
  entry=runtime.model_entries()[1];path=folder/entry['filename'];path.write_bytes(b'broken')
  with patch.object(runtime,'SKILL',root/'skill'),self.assertRaises(ValueError):runtime.model_path(root,entry)
  self.assertEqual(path.read_bytes(),b'broken')
 def test_all_four_models_are_pinned_and_verified(self):
  paths=runtime.model_paths();self.assertEqual(set(paths),{'skin','hair','landmarks','yunet'})
  self.assertEqual(len({runtime.digest(p) for p in paths.values()}),4)

if __name__=='__main__':unittest.main()
