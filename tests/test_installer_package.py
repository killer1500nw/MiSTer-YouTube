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

    def extract(self, fail_menu=False):
        body = self.text.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        body = body.replace('/media/fat', str(self.card))
        if fail_menu:
            injection = '''
real_rename = os.rename
def fail_staged_menu(source, target):
    if 'YouTubeCRT-install-' in source and source.endswith('/Scripts/YouTubeCRT'):
        raise OSError('injected replacement failure')
    return real_rename(source, target)
os.rename = fail_staged_menu
'''
            body = body.replace('n = 0\n', 'n = 0\n' + injection)
        return subprocess.run(['python3', '-c', body, str(INSTALLER)], check=not fail_menu,
                              capture_output=True, text=True)

    def test_fresh_install_paths_and_payload(self):
        self.extract()
        runtime = self.card / 'Scripts/.config/YouTubeCRT'
        self.assertEqual((runtime / 'VERSION.txt').read_text(), '3.23\n')
        self.assertIn("__version__ = '3.23'", (runtime / 'ytcrt/__init__.py').read_text())
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
        subscriptions = b'\xef\xbb\xbf# My subscriptions\r\nRetro channel | @retro\r\n@another'
        (data / 'subscriptions.txt').write_bytes(subscriptions)
        self.extract()
        backups = list((self.card / 'Scripts/.config').glob('YouTubeCRT-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'menu/custom.txt').read_text(), 'keep this')
        self.assertEqual((data / 'settings.json').read_text(), '{"keep": true}')
        runtime = self.card / 'Scripts/.config/YouTubeCRT'
        (runtime / 'obsolete.py').write_text('stale code')
        (legacy / 'obsolete.sh').write_text('stale launcher')
        self.extract()
        self.assertFalse((runtime / 'obsolete.py').exists())
        self.assertFalse((legacy / 'obsolete.sh').exists())
        self.assertEqual((data / 'subscriptions.txt').read_bytes(), subscriptions)
        self.assertEqual((data / 'settings.json').read_text(), '{"keep": true}')
        self.assertEqual(len(list((self.card / 'Scripts/.config').glob('YouTubeCRT-backup-*'))), 2)

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

    def test_failed_replacement_restores_both_application_folders(self):
        self.extract()
        runtime = self.card / 'Scripts/.config/YouTubeCRT'
        menu = self.card / 'Scripts/YouTubeCRT'
        (runtime / 'previous.txt').write_text('old runtime')
        (menu / 'previous.txt').write_text('old menu')
        result = self.extract(fail_menu=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('injected replacement failure', result.stderr)
        self.assertEqual((runtime / 'previous.txt').read_text(), 'old runtime')
        self.assertEqual((menu / 'previous.txt').read_text(), 'old menu')
        self.assertFalse(list((self.card / 'Scripts/.config').glob('YouTubeCRT-install-*')))


if __name__ == '__main__':
    unittest.main()
