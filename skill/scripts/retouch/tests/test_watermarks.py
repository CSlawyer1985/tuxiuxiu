import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image, ImageCms, ImageDraw

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from retouch import watermarks
from retouch.common import artifact
from retouch.runner import Runner
from retouch.store import Store


def make_manifest(project):
    directory=project/'scratch/watermark';directory.mkdir(parents=True,exist_ok=True)
    candidates=[]
    for index,kind in enumerate(['text_only','with_logo','creative']):
        image=Image.new('RGBA',(160,40),(0,0,0,0));draw=ImageDraw.Draw(image)
        draw.rectangle((5+index*12,8,145,32),fill=(255,255-index*80,255,255))
        image.save(directory/f'{kind}.png')
        candidates.append({'id':kind,'kind':kind,'label':kind,'path':f'scratch/watermark/{kind}.png'})
    return {'candidates':candidates}


class WatermarkTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='retouch-watermarks-'))
        self.store=Store(self.root);self.store.initialize({'activity_name':'三版水印合成测试','no_vision':True,'callback_enabled':False})
        folder=self.root/'sources';folder.mkdir()
        icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB',(240,160),(70,80,90)).save(folder/'test.png',icc_profile=icc)
        self.runner=Runner(self.store);self.runner.import_folder(folder);self.ready()
        self.manifest=make_manifest(self.root)

    def ready(self):
        for _ in range(25):
            if self.store.snapshot()['status']=='needs_selection':return self.store.snapshot()
            self.runner.tick()
        self.fail('Sample generation did not complete')

    def choose(self,identifier='with_logo'):
        watermarks.register(self.store,self.manifest);state=self.ready()
        watermarks.select(self.store,state['revision'],identifier)
        return self.ready()

    def test_three_options_have_registered_photo_previews_without_approval(self):
        watermarks.register(self.store,self.manifest);state=self.ready()
        self.assertEqual(len(state['config']['watermark_options']),3)
        for option in state['config']['watermark_options']:
            with Image.open(self.root/option['previews'][0]['image']['path']) as im:self.assertEqual(im.size,(240,160))
        self.assertIsNone(state['config'].get('watermark'))
        self.assertFalse(state['photos'][0].get('approval'))
        self.assertFalse(list((self.root/'output').glob('*')))

    def test_kinds_duplicates_transparency_and_outside_paths_are_rejected(self):
        from copy import deepcopy
        bad=deepcopy(self.manifest);bad['candidates'][0]['kind']='with_logo'
        with self.assertRaises(ValueError):watermarks.register(self.store,bad)
        bad=deepcopy(self.manifest);bad['candidates'][1]['path']=bad['candidates'][0]['path']
        with self.assertRaises(ValueError):watermarks.register(self.store,bad)
        bad=deepcopy(self.manifest);bad['candidates'][0]['path']='../outside.png'
        with self.assertRaises(ValueError):watermarks.register(self.store,bad)
        Image.new('RGBA',(160,40),'white').save(self.root/self.manifest['candidates'][0]['path'])
        with self.assertRaises(ValueError):watermarks.register(self.store,self.manifest)

    def test_selection_invalidates_old_binding_and_rebuilds_full_watermarked_samples(self):
        watermarks.register(self.store,self.manifest);old=self.ready();old_generation=old['photos'][0]['generation']
        result=watermarks.select(self.store,old['revision'],'with_logo')
        self.assertFalse(result['photos_approved'])
        with self.assertRaises(ValueError):self.store.decide(old['revision'],old['sample_binding'],{'profile':'natural'},'stale-watermark-choice')
        state=self.ready();photo=state['photos'][0]
        self.assertGreater(photo['generation'],old_generation)
        self.assertEqual(state['config']['watermark_selection'],'with_logo')
        with Image.open(self.root/photo['candidates']['natural']['watermarked_inspection']['path']) as im:self.assertEqual(im.size,(240,160))
        self.assertFalse(photo.get('approval'))

    def test_selected_asset_and_layout_are_bound_to_final_confirmation_and_export(self):
        state=self.choose()
        choice={'profile':'natural','watermark':True,'watermark_option_id':'with_logo'}
        result=self.store.decide(state['revision'],state['sample_binding'],choice,'watermark-human-approved')
        record=json.loads((self.root/result['decision_path']).read_text())
        self.assertEqual(record['watermark_snapshot'],state['config']['watermark'])
        self.assertEqual(record['choice']['watermark_option_id'],'with_logo')
        for _ in range(8):self.runner.tick()
        completed=self.store.snapshot();self.assertEqual(completed['status'],'completed')
        self.assertTrue(completed['photos'][0].get('output'))
        with self.assertRaises(ValueError):watermarks.select(self.store,completed['revision'],'creative')

    def test_wrong_option_and_asset_drift_cannot_be_approved(self):
        state=self.choose()
        with self.assertRaises(ValueError):self.store.decide(state['revision'],state['sample_binding'],
             {'profile':'natural','watermark':True,'watermark_option_id':'creative'},'wrong-watermark-choice')
        with self.assertRaises(ValueError):watermarks.select(self.store,state['revision'],'unknown')
        (self.root/state['config']['watermark']['path']).write_bytes(b'changed synthetic asset')
        with self.assertRaises(ValueError):self.store.decide(state['revision'],state['sample_binding'],
             {'profile':'natural','watermark':True,'watermark_option_id':'with_logo'},'changed-watermark-choice')

    def test_layout_drift_after_confirmation_blocks_export(self):
        state=self.choose()
        self.store.decide(state['revision'],state['sample_binding'],
             {'profile':'natural','watermark':True,'watermark_option_id':'with_logo'},'layout-drift-approval')
        self.store.mutate(lambda d:d['config']['watermark'].update(width_fraction=.29))
        for _ in range(6):self.runner.tick()
        self.assertFalse(self.store.snapshot()['photos'][0].get('output'))
        self.assertIn('水印与确认记录不符',self.store.snapshot()['photos'][0]['error'])

    def test_changing_selected_draft_rebuilds_without_export_or_approval(self):
        first=self.choose('with_logo');old=first['photos'][0]['candidates']['natural']['watermarked_preview']['sha256']
        watermarks.select(self.store,first['revision'],'creative');second=self.ready()
        self.assertNotEqual(old,second['photos'][0]['candidates']['natural']['watermarked_preview']['sha256'])
        self.assertIsNone(second['photos'][0]['approval'])
        self.assertFalse(list((self.root/'output').glob('*')))
        self.assertEqual(len(list((self.root/'input/activity-materials').glob('水印选版_*.json'))),2)

    def test_provided_watermark_is_not_replaced_and_no_watermark_choice_is_allowed(self):
        ref=artifact(self.root,self.root/self.manifest['candidates'][0]['path'],'watermark')
        self.store.mutate(lambda d:d['config'].update(watermark=ref))
        with self.assertRaises(ValueError):watermarks.register(self.store,self.manifest)
        self.store.mutate(lambda d:d['config'].pop('watermark'))
        watermarks.register(self.store,self.manifest);state=self.ready()
        self.store.decide(state['revision'],state['sample_binding'],{'profile':'natural','watermark':False},'explicit-no-watermark')
        self.assertFalse(self.store.snapshot()['choice']['watermark'])

    def test_http_selection_uses_current_activity_and_does_not_approve_photos(self):
        import http.client
        import threading
        from retouch.service import create_server
        watermarks.register(self.store,self.manifest);state=self.ready()
        server=create_server(self.store,0,start_worker=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            port=server.server_address[1];connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            connection.request('GET','/api/state');response=connection.getresponse();data=json.loads(response.read())
            cookie=response.getheader('Set-Cookie').split(';')[0]
            connection.request('POST','/api/watermark-selection',json.dumps({'revision':state['revision'],'option_id':'creative'}),
                {'Content-Type':'application/json','Cookie':cookie,'Origin':f'http://127.0.0.1:{port}','X-CSRF-Token':data['csrf']})
            response=connection.getresponse();result=json.loads(response.read());connection.close()
            self.assertEqual(response.status,200);self.assertFalse(result['photos_approved'])
            self.assertEqual(self.store.snapshot()['config']['watermark_selection'],'creative')
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
