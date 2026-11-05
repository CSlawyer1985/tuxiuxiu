import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


class CallbackTests(unittest.TestCase):
    def setUp(self):
        try:self.api=importlib.import_module('retouch.callback')
        except ModuleNotFoundError:self.fail('Missing durable conversation callback')
        from retouch.store import Store
        self.store=Store(Path(tempfile.mkdtemp(prefix='retouch-callback-test-')))
        self.store.initialize({'activity_name':'合成回传测试','conversation_id':'00000000-0000-0000-0000-000000000001','callback_enabled':True})
        self.store.event('completed',{'photo_count':3},'event-001',notify=True)

    def test_queue_success_does_not_claim_wakeup_success(self):
        def run(args,**kwargs):return subprocess.CompletedProcess(args,0,'Message queued','Warning: could not resume active thread')
        self.api.dispatch_outbox(self.store,lambda target,text:self.api.queue_message(target,text,run=run))
        row=self.store.outbox()[0]
        import json
        self.assertEqual(row['status'],'queued')
        self.assertFalse(json.loads(row['receipt'])['wake_verified'])

    def test_uncertain_delivery_is_not_blindly_retried(self):
        count=[]
        def transport(target,text):count.append(1);raise TimeoutError('unknown delivery result')
        self.api.dispatch_outbox(self.store,transport)
        self.api.dispatch_outbox(self.store,transport)
        self.assertEqual(self.store.outbox()[0]['status'],'delivery_unknown')
        self.assertEqual(len(count),1)

    def test_queued_event_is_not_sent_twice(self):
        count=[]
        def transport(target,text):count.append(1);return {'status':'queued','queued':True,'wake_verified':False}
        self.api.dispatch_outbox(self.store,transport);self.api.dispatch_outbox(self.store,transport)
        self.assertEqual(len(count),1)
        self.assertEqual(self.store.outbox()[0]['status'],'queued')


if __name__=='__main__':unittest.main()
