import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import setup_runtime as runtime

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='photo-setup-test-')).resolve()

    def test_doctor_missing_environment_does_not_install_or_download(self):
        with patch.object(runtime.urllib.request,'urlopen',side_effect=AssertionError('must not download')):
            self.assertFalse(runtime.doctor(self.root)['ready'])

    def test_confirmed_renderer_binding_changes_with_model_manifest(self):
        from retouch import common
        before=common.renderer_signature();real=common.sha256
        with patch.object(common,'sha256',side_effect=lambda p:'different-model' if Path(p).name=='skin-model.json' else real(p)):
            self.assertNotEqual(before,common.renderer_signature())

    def test_corrupt_bundled_model_is_not_ignored_or_overwritten(self):
        skill=self.root/'skill';folder=skill/'assets/models';folder.mkdir(parents=True)
        (folder/runtime.MODEL['filename']).write_bytes(b'corrupt')
        with patch.object(runtime,'SKILL',skill),self.assertRaises(ValueError):runtime.model_path(self.root)
        self.assertEqual((folder/runtime.MODEL['filename']).read_bytes(),b'corrupt')

    def test_receipt_cannot_escape_environment_folder(self):
        (self.root/'active.json').write_text(json.dumps({'environment':'../../elsewhere'}))
        with self.assertRaises(ValueError):runtime.active_runtime(self.root)

    def test_offline_wheel_tampering_stops_before_install(self):
        skill=self.root/'skill';folder=skill/'assets/wheelhouse';folder.mkdir(parents=True)
        (skill/'assets/runtime-requirements.txt').write_text('example==1\n')
        (folder/'package.whl').write_bytes(b'changed')
        (folder/'manifest.json').write_text(json.dumps({'platform':sys.platform,'machine':runtime.platform.machine(),
            'python':f'{sys.version_info.major}.{sys.version_info.minor}','requirements_sha256':runtime.digest(skill/'assets/runtime-requirements.txt'),
            'files':[{'name':'package.whl','sha256':'wrong'}]}))
        with patch.object(runtime,'SKILL',skill),self.assertRaises(ValueError):runtime.wheelhouse_for_platform()

    def test_offline_platform_mismatch_is_not_used(self):
        skill=self.root/'skill';folder=skill/'assets/wheelhouse';folder.mkdir(parents=True)
        (skill/'assets/runtime-requirements.txt').write_text('example==1\n')
        (folder/'manifest.json').write_text(json.dumps({'platform':'different','machine':'different','python':'0.0',
            'requirements_sha256':runtime.digest(skill/'assets/runtime-requirements.txt'),'files':[]}))
        with patch.object(runtime,'SKILL',skill):self.assertIsNone(runtime.wheelhouse_for_platform())

if __name__=='__main__':unittest.main()
