from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image,ImageCms

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from retouch import retention
from retouch.common import sha256,verify_artifacts
from retouch.runner import Runner
from retouch.store import Store


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='retouch-retention-'))
        self.source=self.root/'source';self.source.mkdir();self.trash=self.root/'trash';self.trash.mkdir()
        self.project=self.root/'activity';self.store=Store(self.project)
        self.store.initialize({'activity_name':'归档续跑合成测试','no_vision':True,'callback_enabled':False})
        self.project=self.store.project
        self.icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        self.add_image('first.png',(90,100,110))
        self.runner=Runner(self.store);self.runner.import_folder(self.source);self.tick_until('needs_selection')

    def add_image(self,name,color):
        Image.new('RGB',(240,160),color).save(self.source/name,icc_profile=self.icc)

    def tick_until(self,status):
        for _ in range(40):
            if self.store.snapshot()['status']==status:return self.store.snapshot()
            if not self.runner.tick():break
        self.assertEqual(self.store.snapshot()['status'],status)
        return self.store.snapshot()

    def approve_and_finish(self):
        state=self.store.snapshot()
        result=self.store.decide(state['revision'],state['sample_binding'],{'profile':'natural','watermark':False},'retention-approval')
        self.record=self.project/result['decision_path'];self.record_bytes=self.record.read_bytes()
        return self.tick_until('completed')

    def test_planning_is_read_only_and_never_targets_originals_or_outputs(self):
        state=self.approve_and_finish();old=state['photos'][0]['candidates']['portrait']['master']
        _,_,plan=retention.plan(self.store)
        self.assertTrue(plan['files']);self.assertTrue((self.project/old['path']).exists())
        self.assertFalse(any(p['path'].startswith(('input/','output/')) for p in plan['files']))

    def test_compaction_preserves_selected_chain_records_and_actual_trash_bytes(self):
        before=self.approve_and_finish();output=before['photos'][0]['output'];rejected=before['photos'][0]['candidates']['portrait']['master']
        result=retention.compact(self.store,self.trash);state=self.store.snapshot()
        self.assertEqual(set(state['photos'][0]['candidates']),{'natural'})
        self.assertEqual(sha256(self.project/output['path']),output['sha256'])
        self.assertEqual(self.record.read_bytes(),self.record_bytes)
        self.assertFalse((self.project/rejected['path']).exists())
        self.assertEqual(sha256(Path(result['trash_directory'])/rejected['path']),rejected['sha256'])
        verify_artifacts(self.project,state['selection_artifacts'])
        self.assertTrue(state['photos'][0]['retained_selected'])

    def test_clean_restart_then_covered_addition_inherits_confirmed_recipe(self):
        old=self.approve_and_finish();retention.compact(self.store,self.trash)
        self.store.mutate(lambda d:d.update(sample_executor='prior-ui-version'))
        self.runner=Runner(Store(self.project))
        self.assertEqual(self.store.snapshot()['choice']['profile'],'natural')
        self.add_image('new.png',(91,101,111));self.runner.import_folder(self.source)
        state=self.tick_until('completed')
        self.assertEqual(len(state['photos']),2)
        self.assertTrue(state['photos'][1].get('scope_inheritance'))
        self.assertEqual(state['photos'][0]['output'],old['photos'][0]['output'])
        self.assertEqual(state['photos'][1]['selected_profile'],'natural')

    def test_uncovered_addition_still_requires_its_own_sample(self):
        self.approve_and_finish();retention.compact(self.store,self.trash)
        self.add_image('dark.png',(1,2,3));self.runner=Runner(self.store);self.runner.import_folder(self.source)
        state=self.tick_until('needs_review')
        self.assertFalse(state['photos'][1].get('output'))
        self.assertTrue(state['photos'][1]['requires_new_sample'])
        self.assertEqual(len(state['photos'][1]['candidates']),4)

    def test_effective_renderer_change_does_not_inherit_old_approval(self):
        self.approve_and_finish();retention.compact(self.store,self.trash)
        self.store.mutate(lambda d:d.update(sample_executor='prior-version'))
        with patch('retouch.runner.renderer_signature',return_value='different-renderer'):Runner(self.store)
        self.assertIsNone(self.store.snapshot()['choice'])

    def test_missing_live_dependency_and_symlink_stop_cleanup(self):
        state=self.approve_and_finish()
        outside=self.root/'external';outside.mkdir();(outside/'keep.txt').write_text('keep')
        link=self.project/'scratch/masters';link.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):retention.compact(self.store,self.trash)
        self.assertEqual((outside/'keep.txt').read_text(),'keep')
        self.assertEqual(self.record.read_bytes(),self.record_bytes)
        self.assertTrue((self.project/state['photos'][0]['candidates']['portrait']['master']['path']).exists())

    def test_interrupted_move_keeps_live_chain_and_can_resume(self):
        self.approve_and_finish();original=retention.os.rename;count=0
        def stop_after_one(source,target):
            nonlocal count
            count+=1
            if count==2:raise OSError('synthetic interrupted move')
            return original(source,target)
        with patch('retouch.retention.os.rename',side_effect=stop_after_one):
            with self.assertRaises(OSError):retention.compact(self.store,self.trash)
        state=self.store.snapshot();self.assertTrue(state.get('retention_pending'))
        verify_artifacts(self.project,state['selection_artifacts'])
        result=retention.compact(self.store,self.trash)
        self.assertTrue(result['trash_verified']);self.assertIsNone(self.store.snapshot()['retention_pending'])

    def test_auto_retention_runs_after_completion_once(self):
        self.approve_and_finish();self.store.mutate(lambda d:d['config'].update(retention_policy='selected_only'))
        original=retention.compact
        with patch('retouch.retention.compact',side_effect=lambda store:original(store,self.trash)) as mocked:
            self.runner.tick();self.runner.tick()
            self.assertEqual(mocked.call_count,1)
        self.assertTrue(self.store.snapshot()['retention_last']['trash_verified'])

    def test_unconfirmed_work_is_not_compacted(self):
        with self.assertRaises(ValueError):retention.compact(self.store,self.trash)
        self.assertEqual(list(self.trash.iterdir()),[])

    def test_legacy_continuation_migration_verifies_original_confirmation(self):
        self.approve_and_finish()
        self.store.mutate(lambda d:d.update(choice=None,confirmed_pipeline=None))
        retention.adopt_existing_confirmation(self.store)
        self.assertEqual(self.store.snapshot()['choice']['profile'],'natural')
        self.assertEqual(self.record.read_bytes(),self.record_bytes)
        self.store.mutate(lambda d:d['photos'][0]['candidates']['natural']['report']['recipe'].update(exposure_ev=.5))
        with self.assertRaises(ValueError):retention.adopt_existing_confirmation(self.store)

    def test_region_manifest_dependencies_are_kept_for_future_photo_analysis(self):
        from retouch.common import artifact,save_json
        self.approve_and_finish();mask=self.project/'scratch/masks/prior.png';mask.parent.mkdir();Image.new('L',(240,160),255).save(mask)
        relative='input/activity-materials/prior-regions.json'
        save_json(self.project/relative,{'protection':artifact(self.project,mask,'mask')})
        self.store.mutate(lambda d:d['config'].update(regions_file=relative))
        retention.compact(self.store,self.trash)
        self.assertTrue(mask.is_file())


if __name__=='__main__':unittest.main()
