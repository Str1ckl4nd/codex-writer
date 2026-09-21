"""Exercise transient packaging with a fake entry point inside the isolated VM."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import remote_bootstrap as bootstrap


class RemotePackage(unittest.TestCase):
    def package(self):
        code="import os,json,sys\nfrom pathlib import Path\nroot=Path(__file__).parent\nprint(json.dumps({'root':str(root),'argv':sys.argv[1:],'config':json.loads(Path(os.environ['SESSION_WRITER_CONFIG']).read_text())}))\n"
        files={name:base64.b64encode((code if name=='session_writer.py' else '').encode()).decode() for name in bootstrap.PROGRAM_FILES}
        return dict(files=files,config={'enable_handoff':False},argv=['snapshot'])

    def test_temporary_backend_runs_without_install_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as root:
            result=subprocess.run([os.sys.executable,bootstrap.__file__],input=json.dumps(self.package()),
                                  text=True,capture_output=True,env=dict(os.environ,TMPDIR=root))
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout)
            self.assertEqual(value['argv'],['snapshot'])
            self.assertEqual(value['config'],{'enable_handoff':False})
            self.assertFalse(Path(value['root']).exists())
            self.assertEqual(list(Path(root).iterdir()),[])

    def test_package_rejects_paths_credentials_and_unknown_operations(self):
        for kind in ('path','credential','operation'):
            value=self.package()
            if kind=='path': value['files']['../unexpected.py']=''
            elif kind=='credential': value['config']['password']='must-not-travel'
            else: value['argv']=['arbitrary-command']
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                bootstrap.decode_package(value)

    def test_windows_installer_includes_the_standalone_backend(self):
        root=Path(bootstrap.__file__).parent.parent
        installer=(root/'scripts/install-windows.ps1').read_text()
        panel=(root/'app/unlock.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("-Filter '*.py'",installer)
        self.assertIn('Get-RemoteBackendPackage',panel)
        self.assertIn('remote_bootstrap.py',panel)


if __name__=='__main__':
    unittest.main()
