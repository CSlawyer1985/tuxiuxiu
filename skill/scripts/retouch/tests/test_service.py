import http.client
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        try:self.api=importlib.import_module('retouch.service')
        except ModuleNotFoundError:self.fail('Missing local review service')
        from retouch.store import Store
        self.root=Path(tempfile.mkdtemp(prefix='retouch-http-test-'))
        self.store=Store(self.root);self.store.initialize({'activity_name':'本地审片测试','no_vision':True})
        self.server=self.api.create_server(self.store,port=0,start_worker=False)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_address[1]
        self.cookie=None;self.csrf=None

    def tearDown(self):
        if hasattr(self,'server'):
            self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

    def request(self,method,path,body=None,origin=True,csrf=True):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        headers={}
        if self.cookie:headers['Cookie']=self.cookie
        if method=='POST':
            headers['Content-Type']='application/json'
            if origin:headers['Origin']=f'http://127.0.0.1:{self.port}'
            if csrf and self.csrf:headers['X-CSRF-Token']=self.csrf
        connection.request(method,path,json.dumps(body).encode() if body is not None else None,headers)
        response=connection.getresponse();data=response.read()
        if response.getheader('Set-Cookie'):self.cookie=response.getheader('Set-Cookie').split(';')[0]
        result=(response.status,data,dict(response.getheaders()));connection.close();return result

    def auth(self):
        status,data,_=self.request('GET','/api/state');self.assertEqual(status,200)
        self.csrf=json.loads(data)['csrf']

    def test_state_is_readable_and_page_has_local_assets(self):
        self.auth();status,data,headers=self.request('GET','/')
        self.assertEqual(status,200);self.assertIn(b'/app.js',data)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])

    def test_cross_origin_or_missing_csrf_cannot_mutate(self):
        self.auth()
        self.assertEqual(self.request('POST','/api/control',{'action':'pause'},origin=False)[0],403)
        self.assertEqual(self.request('POST','/api/control',{'action':'pause'},csrf=False)[0],403)
        self.assertNotEqual(self.store.snapshot()['status'],'paused')

    def test_registered_media_only_and_no_path_traversal(self):
        self.auth()
        self.assertEqual(self.request('GET','/media/../../AGENTS.md')[0],404)
        self.assertEqual(self.request('GET','/media/unregistered')[0],404)

    def test_pause_and_resume_persist_from_http(self):
        self.auth()
        self.assertEqual(self.request('POST','/api/control',{'action':'pause'})[0],200)
        self.assertEqual(self.store.snapshot()['status'],'paused')
        self.assertEqual(self.request('POST','/api/control',{'action':'resume'})[0],200)
        self.assertEqual(self.store.snapshot()['status'],'preparing')

    def test_two_activities_do_not_replace_each_others_cookie(self):
        self.auth();first_name=self.cookie.split('=')[0]
        from retouch.store import Store
        other=Store(self.root/'second');other.initialize({'activity_name':'第二场合成测试','no_vision':True})
        server=self.api.create_server(other,0,start_worker=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            connection.request('GET','/api/state',headers={'Cookie':self.cookie});response=connection.getresponse();response.read()
            second_cookie=response.getheader('Set-Cookie').split(';')[0];connection.close()
            self.assertNotEqual(first_name,second_cookie.split('=')[0])
            self.cookie+='; '+second_cookie
            self.assertEqual(self.request('POST','/api/control',{'action':'pause'})[0],200)
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def photo_ready(self):
        from PIL import Image,ImageCms
        folder=self.root/'fixture';folder.mkdir()
        Image.new('RGB',(120,80),(90,80,70)).save(folder/'test.png',icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        self.server.runner.import_folder(folder)
        for _ in range(15):self.server.runner.tick()
        self.auth();return self.store.snapshot()

    def test_tuning_preview_changes_pixels_without_approving_or_exporting(self):
        import base64,io
        from PIL import Image
        state=self.photo_ready();photo=state['photos'][0]
        status,data,_=self.request('POST','/api/tuning-preview',{'photo_id':photo['id'],'revision':state['revision'],
             'profile':'clear','adjustments':{'exposure_ev':.3},'watermark':False})
        self.assertEqual(status,200,data)
        image=Image.open(io.BytesIO(base64.b64decode(json.loads(data)['image_url'].split(',',1)[1])))
        self.assertGreater(image.getpixel((50,40))[0],90)
        self.assertEqual(self.store.snapshot()['status'],'needs_selection')
        self.assertFalse(list((self.root/'output').glob('*')))

    def test_tuning_endpoint_saves_new_sample_parameters(self):
        state=self.photo_ready()
        status,data,_=self.request('POST','/api/tuning',{'revision':state['revision'],'profile':'clear',
            'adjustments':{'exposure_ev':.2},'scope':'batch'})
        self.assertEqual(status,200,data)
        self.assertEqual(self.store.snapshot()['photos'][0]['tuning']['clear']['exposure_ev'],.2)

    def test_revision_request_endpoint_keeps_note_as_pending_work(self):
        state=self.photo_ready()
        status,data,_=self.request('POST','/api/revision-request',{'revision':state['revision'],'scope':'current',
               'photo_id':state['photos'][0]['id'],'text':'脸部再亮一点'})
        self.assertEqual(status,200,data)
        self.assertEqual(self.store.snapshot()['pending_request']['text'],'脸部再亮一点')


if __name__=='__main__':unittest.main()
