"""Exercise the packaged installer without touching an SD card or starting services."""
import base64
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


INSTALLER = Path(__file__).resolve().parents[1] / 'release' / 'Install_YouTube_CRT.sh'


class InstallerPackage(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.card = Path(self.scratch.name)
        self.text = INSTALLER.read_text('ascii')

    def extract(self):
        body = self.text.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        body = body.replace('/media/fat', str(self.card))
        subprocess.run(['python3', '-c', body, str(INSTALLER)], check=True,
                       capture_output=True, text=True)

    def test_fresh_install_paths_and_payload(self):
        self.extract()
        runtime = self.card / 'Scripts/.config/YouTubeCRT'
        self.assertEqual((runtime / 'VERSION.txt').read_text(), '3.25\n')
        self.assertIn("__version__ = '3.25'", (runtime / 'ytcrt/__init__.py').read_text())
        self.assertTrue((runtime / 'YouTubeCRT_v0.16-fb.rbf').is_file())
        menu = list((self.card / 'Scripts/YouTubeCRT').iterdir())
        self.assertEqual(len(menu), 7)
        self.assertTrue(all(p.suffix == '.sh' for p in menu))
        self.assertFalse((self.card / 'YouTubeNative').exists())
        blob = self.text.split('\n#ZIP-BEGIN\n', 1)[1].split('\n#ZIP-END', 1)[0]
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(''.join(
                line[1:] for line in blob.splitlines()), validate=True))) as payload:
            self.assertIsNone(payload.testzip())
            for name in payload.namelist():
                self.assertEqual((self.card / name).read_bytes(), payload.read(name))

    def test_existing_install_is_preserved_and_rerun_is_idempotent(self):
        legacy = self.card / 'Scripts/YouTubeCRT'
        (legacy / 'ytcrt').mkdir(parents=True)
        (legacy / 'youtube_crt.py').write_text('old player')
        (legacy / 'custom.txt').write_text('keep this')
        data = self.card / 'YouTubeNative'
        data.mkdir()
        (data / 'settings.json').write_text('{"keep": true}')
        self.extract()
        backups = list((self.card / 'Scripts/.config').glob('YouTubeCRT-legacy-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'YouTubeCRT/custom.txt').read_text(), 'keep this')
        self.assertEqual((data / 'settings.json').read_text(), '{"keep": true}')
        self.extract()
        self.assertEqual(len(list((self.card / 'Scripts/.config').glob('YouTubeCRT-legacy-*'))), 1)

    def test_menu_launcher_forwards_arguments_and_exit_status(self):
        self.extract()
        helper = self.card / 'Scripts/.config/YouTubeCRT/Status_YouTube_CRT.sh'
        helper.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\nexit 7\n')
        launcher = self.card / 'Scripts/YouTubeCRT/Status_YouTube_CRT.sh'
        result = subprocess.run(['bash', '-c', launcher.read_text().replace(
            '/media/fat', str(self.card)), 'launcher', 'two words', '--test'],
            capture_output=True, text=True)
        self.assertEqual(result.stdout, 'two words\n--test\n')
        self.assertEqual(result.returncode, 7)


if __name__ == '__main__':
    unittest.main()
