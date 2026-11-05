import importlib
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image,ImageCms

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


class TuningTests(unittest.TestCase):
    def setUp(self):
        from retouch.store import Store
        from retouch.runner import Runner
        self.root=Path(tempfile.mkdtemp(prefix='retouch-tuning-test-')).resolve()
        self.source=self.root/'source';self.source.mkdir()
        icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        for i in range(2):Image.new('RGB',(120,80),(90+i*15,80+i*10,70+i*8)).save(self.source/f'{i}.png',icc_profile=icc)
        self.store=Store(self.root/'activity');self.store.initialize({'activity_name':'参数合成测试','no_vision':True,'callback_enabled':False})
        self.runner=Runner(self.store);self.runner.import_folder(self.source)
        self.run_until('needs_selection')

    def api(self):
        try:return importlib.import_module('retouch.tuning')
        except ModuleNotFoundError:self.fail('Missing actual tuning contract')

    def run_until(self,status):
        for _ in range(35):
            if self.store.snapshot()['status']==status:return self.store.snapshot()
            self.runner.tick()
        self.fail('workflow did not reach '+status)

    def test_tuning_rejects_unknown_nonfinite_and_out_of_range_values(self):
        tuning=self.api()
        for bad in ({'unknown':1},{'exposure_ev':True},{'exposure_ev':float('nan')},{'skin_smooth':1.01},[]):
            with self.subTest(bad=bad),self.assertRaises(ValueError):tuning.validate(bad)

    def test_exposure_and_color_controls_change_actual_pixels(self):
        from retouch import engine
        tuning=self.api();image=Image.new('RGB',(32,24),(100,80,60))
        base=engine.plan_recipe('keep',{})
        brighter,_=engine.render(image,tuning.apply(base,{'exposure_ev':.3}),None,None)
        warmer,_=engine.render(image,tuning.apply(base,{'warmth':3.}),None,None)
        vivid,_=engine.render(image,tuning.apply(base,{'saturation':.25}),None,None)
        self.assertGreater(np.asarray(brighter).mean(),np.asarray(image).mean())
        self.assertGreater(float(engine.rgb_to_lab(np.asarray(warmer)/255)[...,2].mean()),float(engine.rgb_to_lab(np.asarray(image)/255)[...,2].mean()))
        self.assertFalse(np.array_equal(np.asarray(vivid),np.asarray(image)))

    def test_every_slider_endpoint_is_reachable_for_each_adaptive_profile(self):
        from retouch import engine
        tuning=self.api()
        for profile in ('natural','clear','portrait'):
            for reference in (20,100,200):
                base=engine.plan_recipe(profile,{'reference_p50':reference,'whole':{'bright_fraction':.1}})
                for control in tuning.CONTROLS:
                    for value in (control['min'],control['max']):
                        with self.subTest(profile=profile,reference=reference,key=control['key'],value=value):
                            tuning.apply(base,{control['key']:value})

    def test_saved_tuning_rebuilds_candidate_and_invalidates_old_confirmation(self):
        old=self.store.snapshot();before=old['photos'][0]['candidates']['clear']['master']['sha256']
        self.runner.queue_tuning(old['revision'],'clear',{'exposure_ev':.3},'batch')
        with self.assertRaises(ValueError):self.store.decide(old['revision'],old['sample_binding'],{'profile':'clear'},'old-tune-request')
        new=self.run_until('needs_selection')
        self.assertNotEqual(before,new['photos'][0]['candidates']['clear']['master']['sha256'])
        self.assertEqual(new['photos'][0]['tuning']['clear']['exposure_ev'],.3)
        self.assertFalse(list((self.store.project/'output').glob('*')))

    def test_current_tuning_preserves_other_completed_outputs(self):
        d=self.store.snapshot();self.store.decide(d['revision'],d['sample_binding'],{'profile':'natural'},'initial-tuning-approval')
        old=self.run_until('completed');saved=[p['output'] for p in old['photos']]
        self.runner.queue_tuning(old['revision'],'clear',{'exposure_ev':.2},'current',old['photos'][0]['id'])
        for output in saved:self.assertTrue((self.store.project/output['path']).is_file())
        self.assertEqual(self.store.snapshot()['photos'][1]['status'],'completed')
        self.assertFalse(self.store.snapshot()['photos'][0].get('approval'))

    def test_change_note_cannot_silently_approve_old_pixels(self):
        d=self.store.snapshot()
        with self.assertRaises(ValueError):self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear','note':'不够亮，脸部再亮一点'},'unresolved-note-01')
        self.assertEqual(self.store.snapshot()['status'],'needs_selection')

    def test_revision_request_is_persisted_and_stops_affected_export(self):
        d=self.store.snapshot();pid=d['photos'][0]['id']
        result=self.runner.request_changes(d['revision'],'脸部不够亮','current',pid)
        current=self.store.snapshot()
        self.assertEqual(current['pending_request']['text'],'脸部不够亮')
        self.assertEqual(current['pending_request']['photo_ids'],[pid])
        self.assertTrue((self.store.project/result['request_path']).is_file())
        self.assertTrue(any(r['kind']=='revision_request' for r in self.store.outbox()))
        with self.assertRaises(ValueError):self.store.decide(current['revision'],current['sample_binding'],{'profile':'clear'},'unresolved-request')

    def test_confirmed_record_contains_actual_parameter_snapshot(self):
        d=self.store.snapshot();self.runner.queue_tuning(d['revision'],'clear',{'exposure_ev':.2,'skin_smooth':.1},'batch')
        d=self.run_until('needs_selection');response=self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear'},'full-parameter-request')
        from retouch.common import load_json
        record=load_json(self.store.project/response['decision_path'])
        first=d['photos'][0]
        self.assertEqual(record['parameter_snapshot'][first['id']]['adjustments']['exposure_ev'],.2)
        self.assertEqual(record['parameter_snapshot'][first['id']]['parameters']['skin_smooth'],0.)
        self.assertEqual(record['parameter_snapshot'][first['id']]['adjustments']['skin_smooth'],.1)

    def test_executor_update_does_not_reopen_delivered_batch(self):
        from retouch.runner import Runner
        d=self.store.snapshot();self.store.decide(d['revision'],d['sample_binding'],{'profile':'natural'},'completed-before-update')
        old=self.run_until('completed');self.store.mutate(lambda d:d.update(sample_executor='obsolete'))
        Runner(self.store)
        self.assertEqual(self.store.snapshot()['status'],'completed')
        self.assertEqual([p['output'] for p in self.store.snapshot()['photos']],[p['output'] for p in old['photos']])

    def test_confirming_unrelated_photo_does_not_close_revision_request(self):
        d=self.store.snapshot();a,b=[p['id'] for p in d['photos']]
        self.runner.request_changes(d['revision'],'A图再亮一点','current',a)
        d=self.store.snapshot();self.runner.queue_tuning(d['revision'],'clear',{'exposure_ev':.2},'current',a)
        d=self.run_until('needs_selection')
        self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear','approved_photo_ids':[b]},'only-unrelated-B')
        self.assertEqual(self.store.snapshot()['pending_request']['status'],'awaiting_confirmation')
        self.assertIsNone(self.store.snapshot()['photos'][0].get('approval'))

    def test_unsampled_parameter_snapshot_matches_later_actual_render(self):
        self.store.mutate(lambda d:d['photos'][1]['candidates'].pop('clear'))
        d=self.store.snapshot();result=self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear'},'unsampled-params')
        from retouch.common import load_json
        planned=load_json(self.store.project/result['decision_path'])['parameter_snapshot'][d['photos'][1]['id']]['parameters']
        final=self.run_until('completed');actual=final['photos'][1]['candidates']['clear']['report']['recipe']
        self.assertEqual(planned,actual)

    def test_failed_technical_review_cannot_satisfy_mask_gate(self):
        from retouch.common import save_json,artifact
        photo=dict(self.store.snapshot()['photos'][0])
        file=self.store.project/'scratch/failing-technical-review.json'
        save_json(file,{'schema_version':'assistant-technical-mask-review-1','actor':'assistant',
              'binding':{'input':photo['input'],'masks':photo['masks']},'checks':{'technical_result':'fail'}})
        photo['assistant_technical_review']=artifact(self.store.project,file,'review')
        self.assertFalse(self.runner.mask_review_current(photo))

    def test_parameter_snapshot_is_shared_by_reference_not_copied_per_photo(self):
        d=self.store.snapshot();response=self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear'},'shared-params-reference')
        from retouch.common import verify_artifacts,load_json
        self.assertIn('parameter_snapshot_ref',response)
        verify_artifacts(self.store.project,[response['parameter_snapshot_ref']])
        self.assertEqual(len(load_json(self.store.project/response['parameter_snapshot_ref']['path'])['parameter_snapshot']),2)
        for photo in self.store.snapshot()['photos']:
            self.assertNotIn('parameter_snapshot',photo['approval'])
            self.assertEqual(photo['approval']['parameter_snapshot_ref'],response['parameter_snapshot_ref'])

    def test_revision_request_does_not_cancel_explicit_pause(self):
        self.runner.control('pause');d=self.store.snapshot()
        self.runner.request_changes(d['revision'],'再亮一点','current',d['photos'][0]['id'])
        self.assertEqual(self.store.snapshot()['status'],'paused')
        self.assertFalse(self.runner.tick())

    def test_matching_scope_and_new_sample_close_request_after_confirmation(self):
        d=self.store.snapshot();pid=d['photos'][0]['id'];text='A图再亮一点'
        self.runner.request_changes(d['revision'],text,'current',pid)
        d=self.store.snapshot();self.runner.queue_tuning(d['revision'],'clear',{'exposure_ev':.2},'current',pid)
        d=self.run_until('needs_selection')
        self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear','approved_photo_ids':[pid],
             'note':text,'note_intent':'change_request'},'confirmed-request-scope')
        self.assertEqual(self.store.snapshot()['pending_request']['status'],'approved')

    def test_changed_parameter_record_prevents_export(self):
        d=self.store.snapshot();response=self.store.decide(d['revision'],d['sample_binding'],{'profile':'natural'},'tampered-params')
        path=self.store.project/response['parameter_snapshot_ref']['path'];path.write_bytes(path.read_bytes()+b'\n')
        for _ in range(12):self.runner.tick()
        self.assertFalse(any(p['status']=='completed' for p in self.store.snapshot()['photos']))
        self.assertFalse(list((self.store.project/'output').glob('*.jpg')))

    def test_batch_request_can_be_confirmed_photo_by_photo(self):
        d=self.store.snapshot();ids=[p['id'] for p in d['photos']]
        self.runner.request_changes(d['revision'],'本批再亮一点','batch')
        d=self.store.snapshot();self.runner.queue_tuning(d['revision'],'clear',{'exposure_ev':.2},'batch')
        d=self.run_until('needs_selection')
        self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear','approved_photo_ids':[ids[0]]},'request-confirm-A')
        d=self.run_until('needs_review')
        self.store.decide(d['revision'],d['sample_binding'],{'profile':'clear','approved_photo_ids':[ids[1]]},'request-confirm-B')
        d=self.run_until('completed')
        self.assertEqual(d['pending_request']['status'],'approved')
        self.assertEqual(set(d['pending_request']['approved_photo_ids']),set(ids))

    def test_failed_tuning_render_can_retry_without_using_old_approval(self):
        d=self.store.snapshot();self.store.decide(d['revision'],d['sample_binding'],{'profile':'natural'},'before-render-failure')
        d=self.run_until('completed');pid=d['photos'][0]['id']
        self.runner.queue_tuning(d['revision'],'clear',{'exposure_ev':.2},'current',pid)
        d=self.store.snapshot();block=self.store.project/f'scratch/retouch/runs/{d["run_id"]}/r{d["revision"]}'
        block.write_text('retained synthetic path blocker')
        self.run_until('needs_review');block.rename(self.store.project/'scratch/retained-path-blocker')
        self.runner.control('retry',pid)
        for _ in range(20):self.runner.tick()
        photo=self.store.snapshot()['photos'][0]
        self.assertIn('clear',photo['candidates'])
        self.assertIsNone(photo.get('approval'))
        self.assertIsNone(photo.get('output'))
