from pathlib import Path
import shutil
import subprocess
import unittest


class UiRegressionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Optional UI regression requires existing Node; no install')
    def test_shipped_async_handlers_preserve_other_drafts_and_choice_scope(self):
        result=subprocess.run([shutil.which('node'),str(Path(__file__).with_name('ui_regressions.cjs'))],
                              capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
