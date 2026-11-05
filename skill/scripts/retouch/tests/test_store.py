import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class StoreTests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module('retouch.store')
        except ModuleNotFoundError:
            self.fail('Missing durable task store implementation')
        self.project = Path(tempfile.mkdtemp(prefix='retouch-store-test-'))
        self.store = self.api.Store(self.project)
        self.store.initialize({'activity_name': '合成活动', 'conversation_id': None})
        self.artifact = self.project / 'scratch' / 'candidate.txt'
        self.artifact.write_text('approved bytes')
        from retouch.common import sha256
        self.store.mutate(lambda d: d.update(status='needs_selection', sample_binding='sample-1',
            selection_artifacts=[{'path': 'scratch/candidate.txt', 'sha256': sha256(self.artifact)}],
            photos=[{'id': 'p1', 'status': 'sample_ready'}]))

    def choice(self, request='request-001', revision=1):
        return self.store.decide(revision, 'sample-1', {'profile': 'clear', 'watermark': False,
            'skin_reviewed': ['p1'], 'overrides': {}, 'note': ''}, request)

    def test_decision_survives_restart(self):
        self.choice()
        reopened = self.api.Store(self.project).snapshot()
        self.assertEqual(reopened['status'], 'running')
        self.assertEqual(reopened['choice']['profile'], 'clear')
        self.assertEqual(len(list((self.project / 'input/activity-materials').glob('*.json'))), 1)

    def test_repeated_request_is_idempotent(self):
        first = self.choice()
        second = self.choice()
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.outbox()), 1)

    def test_old_revision_is_rejected(self):
        self.store.mutate(lambda d: d.update(revision=2))
        with self.assertRaises(ValueError):
            self.choice()
        self.assertEqual(self.store.snapshot()['status'], 'needs_selection')

    def test_changed_candidate_cannot_be_approved(self):
        self.artifact.write_text('changed after preview')
        with self.assertRaises(ValueError):
            self.choice()
        self.assertFalse(self.store.outbox())

    def test_arbitrary_profile_or_photo_is_rejected(self):
        for choice in ({'profile': 'generate-new-face'}, {'profile': 'natural', 'overrides': {'other': 'clear'}}):
            with self.assertRaises(ValueError):
                self.store.decide(1, 'sample-1', choice, 'invalid-001')

    def test_two_distinct_concurrent_confirmations_only_one_wins(self):
        results = []
        def send(request):
            try:
                self.choice(request)
                results.append('accepted')
            except ValueError:
                results.append('rejected')
        threads = [threading.Thread(target=send, args=(f'concurrent-{i}',)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(results, ['accepted', 'rejected'])

    def test_duplicate_request_with_different_choice_is_rejected(self):
        self.choice()
        with self.assertRaises(ValueError):
            self.store.decide(1, 'sample-1', {'profile': 'portrait'}, 'request-001')

    def test_path_escape_is_rejected(self):
        from retouch.common import inside
        with self.assertRaises(ValueError): inside(self.project, '../outside')
        alias = self.project / 'scratch/alias'
        alias.symlink_to(self.artifact)
        with self.assertRaises(ValueError): inside(self.project, 'scratch/alias')

    def test_single_photo_choice_leaves_others_unapproved(self):
        self.store.mutate(lambda d:d['photos'][0].update(candidates={'natural':{'profile':'natural'}}))
        self.store.mutate(lambda d:d['photos'].append({'id':'p2','status':'sample_ready'}))
        self.store.decide(1,'sample-1',{'profile':'natural','approved_photo_ids':['p1']},'single-photo-001')
        photos=self.store.snapshot()['photos']
        self.assertTrue(photos[0].get('approval'))
        self.assertFalse(photos[1].get('approval'))

    def test_missing_watermark_asset_cannot_be_approved(self):
        with self.assertRaises(ValueError):self.store.decide(1,'sample-1',{'profile':'natural','watermark':True},'missing-mark-001')

    def test_store_rejects_external_scratch_link_before_creating_database(self):
        project=self.project/'unsafe';project.mkdir();outside=self.project/'external';outside.mkdir()
        (project/'scratch').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.api.Store(project)
        self.assertEqual(list(outside.iterdir()),[])


if __name__ == '__main__':
    unittest.main()
