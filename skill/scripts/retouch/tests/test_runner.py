import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from PIL import Image, ImageCms

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        try: self.api=importlib.import_module('retouch.runner')
        except ModuleNotFoundError: self.fail('Missing batch worker')
        from retouch.store import Store
        self.root=Path(tempfile.mkdtemp(prefix='retouch-runner-test-')).resolve()
        self.source=self.root/'source';self.source.mkdir()
        icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        for i in range(3): Image.new('RGB',(120,80),(65+i*30,70+i*20,75+i*10)).save(self.source/f'photo-{i}.png',icc_profile=icc)
        self.project=self.root/'activity';self.store=Store(self.project)
        self.store.initialize({'activity_name':'合成测试','no_vision':True,'callback_enabled':False})
        self.runner=self.api.Runner(self.store);self.runner.import_folder(self.source)

    def ready(self):
        for _ in range(30):
            if self.store.snapshot()['status']=='needs_selection': return self.store.snapshot()
            self.runner.tick()
        self.fail('Samples never became ready')

    def approve(self):
        d=self.ready();self.store.decide(d['revision'],d['sample_binding'],
            {'profile':'clear','watermark':False,'overrides':{},'skin_reviewed':[]},'synthetic-approval')

    def finish(self):
        for _ in range(15):
            if self.store.snapshot()['status'] in {'completed','needs_review'}:return self.store.snapshot()
            self.runner.tick()
        self.fail('Worker did not reach an observable final state')

    def test_samples_are_full_size_and_not_final_outputs(self):
        state=self.ready()
        self.assertEqual(len(state['photos']),3)
        self.assertFalse(list((self.project/'output').glob('*')))
        for photo in state['photos']:
            with Image.open(self.project/photo['candidates']['clear']['master']['path']) as im:self.assertEqual(im.size,(120,80))
            self.assertTrue(photo['candidates']['clear']['details'])

    def test_confirmed_batch_can_continue_after_reopen(self):
        self.approve();self.runner.tick()
        self.runner=self.api.Runner(type(self.store)(self.project))
        state=self.finish()
        self.assertEqual(state['status'],'completed')
        self.assertEqual(sum(p['status']=='completed' for p in state['photos']),3)
        self.assertEqual(len(list((self.project/'output').glob('*.jpg'))),3)
        self.assertEqual(len(list(self.source.glob('*.png'))),3)

    def test_pause_does_not_process_next_photo(self):
        self.approve();self.runner.control('pause');self.assertFalse(self.runner.tick())
        self.assertFalse(list((self.project/'output').glob('*')))
        self.runner.control('resume');self.assertEqual(self.finish()['status'],'completed')

    def test_file_save_failure_does_not_count_as_success(self):
        self.approve();(self.project/'output').write_text('blocks output directory')
        state=self.finish()
        self.assertEqual(state['status'],'needs_review')
        self.assertFalse(any(p['status']=='completed' for p in state['photos']))
        self.assertTrue(any(p.get('error') for p in state['photos']))

    def test_changed_original_after_approval_is_not_rendered(self):
        self.approve();state=self.store.snapshot();path=self.project/state['photos'][0]['input']['path']
        path.write_bytes(b'changed synthetic original')
        state=self.finish()
        self.assertEqual(state['status'],'needs_review')
        self.assertNotEqual(state['photos'][0]['status'],'completed')

    def test_mask_edit_invalidates_old_selection(self):
        old=self.ready();photo=old['photos'][0]
        self.runner.update_masks(photo['id'],old['revision'],photo['input']['sha256'],
            [{'layer':'protect','tool':'rect','points':[[0,0],[.3,.3]],'radius':.02}])
        with self.assertRaises(ValueError):self.store.decide(old['revision'],old['sample_binding'],
            {'profile':'clear'},'stale-approval')

    def test_manual_masks_survive_reanalysis_and_wrong_source_is_refused(self):
        state=self.ready();p=state['photos'][0]
        self.runner.update_masks(p['id'],state['revision'],p['input']['sha256'],
            [{'layer':'skin','tool':'rect','points':[[.1,.1],[.9,.9]],'radius':.02}])
        state=self.store.snapshot();p=state['photos'][0];original=p['masks']
        self.runner._prepare_photo(state,p)
        self.assertEqual(self.store.snapshot()['photos'][0]['masks'],original)
        p=self.store.snapshot()['photos'][0];p['mask_overrides']['input_sha256']='wrong'
        with self.assertRaises(ValueError):self.runner._prepare_photo(self.store.snapshot(),p)

    def test_texture_mask_edit_persists_and_reanalysis_reuses_all_three_masks(self):
        from copy import deepcopy
        state=self.ready();p=state['photos'][0]
        def add_texture(d):d['photos'][0]['masks']['texture']=deepcopy(d['photos'][0]['masks']['skin'])
        self.store.mutate(add_texture);state=self.store.snapshot();p=state['photos'][0]
        self.runner.update_masks(p['id'],state['revision'],p['input']['sha256'],
            [{'layer':'skin','tool':'rect','points':[[.2,.2],[.8,.8]],'radius':.02}])
        state=self.store.snapshot();p=state['photos'][0];refs=deepcopy(p['masks'])
        with Image.open(self.project/refs['texture']['path']) as mask:self.assertEqual(mask.getpixel((50,40)),255)
        self.runner._prepare_photo(state,p)
        self.assertEqual(self.store.snapshot()['photos'][0]['masks'],refs)

    def test_unreviewed_smoothing_is_deferred_in_full_size_and_quick_preview(self):
        state=self.ready();p=state['photos'][0]
        candidate=self.runner._candidate(state,p,'portrait')
        self.assertEqual(candidate['report']['recipe']['skin_smooth'],0)
        self.assertEqual(candidate['report']['deferred_modules'][0]['module'],'skin_smooth')
        _,recipe=self.runner.preview_tuning(p['id'],state['revision'],'portrait',{'skin_smooth':1.},False)
        self.assertEqual(recipe['skin_smooth'],0)

    def test_smoothing_requires_all_four_checks_bound_to_current_masks(self):
        from retouch.common import artifact,save_json
        state=self.ready();p=state['photos'][0];path=self.project/'scratch/full-skin-review.json'
        record={'schema_version':'assistant-technical-mask-review-1','actor':'assistant',
            'binding':{'input':p['input'],'masks':p['masks']},'checks':{'technical_result':True}}
        save_json(path,record);p['assistant_technical_review']=artifact(self.project,path,'report')
        self.assertFalse(self.runner.smoothing_review_current(p),'generic old review cannot unlock smoothing')
        record['checks'].update({key:True for key in ('visible_skin_complete','exclusions_checked','transitions_checked','texture_checked')})
        path=self.project/'scratch/full-skin-review-v2.json';save_json(path,record);p['assistant_technical_review']=artifact(self.project,path,'report')
        self.assertTrue(self.runner.smoothing_review_current(p))
        recipe,deferred=self.runner._safe_smoothing_recipe(p,{'skin_smooth':.5})
        self.assertEqual(recipe['skin_smooth'],.5);self.assertFalse(deferred)
        p['masks']['skin']['sha256']='changed'
        self.assertFalse(self.runner.smoothing_review_current(p))

    def test_old_worker_cannot_restore_a_previous_mask(self):
        state=self.ready();old=state['photos'][0]
        self.runner.update_masks(old['id'],state['revision'],old['input']['sha256'],
            [{'layer':'protect','tool':'rect','points':[[0,0],[.3,.3]],'radius':.02}])
        changed=self.store.snapshot()['photos'][0]['masks']
        with self.assertRaises(ValueError):self.runner._commit_photo(old['id'],old,old['input']['sha256'])
        self.assertEqual(self.store.snapshot()['photos'][0]['masks'],changed)

    def test_same_batch_addition_with_covered_conditions_reuses_approval(self):
        self.approve();before=self.finish();old_outputs=[p['output']['sha256'] for p in before['photos']]
        icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB',(120,80),(92,96,100)).save(self.source/'added.png',icc_profile=icc)
        self.runner.import_folder(self.source)
        state=self.finish()
        self.assertEqual(state['status'],'completed')
        self.assertEqual(sum(p['status']=='completed' for p in state['photos']),4)
        self.assertEqual([p['output']['sha256'] for p in state['photos'][:3]],old_outputs)

    def test_uncovered_addition_requires_its_own_sample(self):
        self.approve();self.finish()
        icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB',(120,80),(1,2,3)).save(self.source/'very-dark.png',icc_profile=icc)
        self.runner.import_folder(self.source)
        state=self.finish()
        self.assertEqual(state['status'],'needs_review')
        self.assertEqual(sum(p['status']=='completed' for p in state['photos']),3)
        self.assertFalse(state['photos'][-1].get('output'))
        self.assertIn('clear',state['photos'][-1]['candidates'])

    def test_unreadable_only_batch_stops_with_visible_error(self):
        from retouch.store import Store
        source=self.root/'invalid';source.mkdir();(source/'broken.jpg').write_bytes(b'broken image')
        store=Store(self.root/'invalid-activity');store.initialize({'activity_name':'坏文件测试','no_vision':True})
        runner=self.api.Runner(store);runner.import_folder(source)
        for _ in range(6):runner.tick()
        self.assertEqual(store.snapshot()['status'],'needs_review')
        self.assertTrue(store.snapshot()['photos'][0]['error'])

    def test_changed_executor_cannot_use_previous_sample_confirmation(self):
        state=self.ready()
        self.store.mutate(lambda d:d.update(sample_executor='obsolete-implementation'))
        with self.assertRaises(ValueError):self.store.decide(state['revision'],state['sample_binding'],{'profile':'clear'},'old-engine-001')

    def test_unsampled_current_photo_cannot_be_approved(self):
        state=self.ready();pid=state['photos'][0]['id']
        self.store.mutate(lambda d:d['photos'][0].update(candidates={}))
        with self.assertRaises(ValueError):self.store.decide(state['revision'],state['sample_binding'],
            {'profile':'portrait','approved_photo_ids':[pid]},'unsampled-001')

    def test_equal_brightness_new_blue_cast_requires_samples(self):
        self.approve();self.finish()
        Image.new('RGB',(120,80),(30,115,160)).save(self.source/'blue.png',icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        self.runner.import_folder(self.source);state=self.finish()
        self.assertEqual(state['photos'][-1]['status'],'needs_review')
        self.assertFalse(state['photos'][-1].get('output'))

    def test_symlink_output_parent_is_rejected_before_external_write(self):
        self.approve();outside=self.root/'outside';outside.mkdir();(self.project/'output').symlink_to(outside,target_is_directory=True)
        self.finish()
        self.assertEqual(list(outside.iterdir()),[])

    def test_excluded_old_output_is_not_in_delivery(self):
        self.approve();before=self.finish();photo=before['photos'][0]
        self.runner.update_masks(photo['id'],before['revision'],photo['input']['sha256'],
            [{'layer':'protect','tool':'rect','points':[[0,0],[.1,.1]],'radius':.02}])
        self.runner.control('exclude',photo['id'],'合成测试跳过修改版')
        state=self.finish()
        from retouch.common import load_json
        delivery=load_json(self.project/state['delivery']['path'])
        self.assertEqual(delivery['photo_count'],2)
        self.assertNotIn(photo['output']['path'],[r['path'] for r in delivery['outputs']])

    def test_reprepare_preserves_completed_files_but_requires_new_choice(self):
        self.approve();before=self.finish();old=before['photos'][0]['output']
        self.runner.control('reprepare',before['photos'][0]['id'])
        for _ in range(15):self.runner.tick()
        state=self.store.snapshot();photo=state['photos'][0]
        self.assertEqual(state['status'],'needs_review')
        self.assertFalse(photo.get('approval'))
        self.assertTrue((self.project/old['path']).is_file())
        self.assertIn('clear',photo['candidates'])


if __name__=='__main__':unittest.main()
