"""Release wiring checks; no network, credentials, or real publication."""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class WindowsReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load(
            (ROOT / '.github/workflows/windows-installer.yml').read_text(encoding='utf-8'),
            Loader=yaml.BaseLoader,
        )
        cls.jobs = cls.workflow['jobs']

    def test_triggers_and_permissions(self):
        self.assertEqual(self.workflow['on']['push']['tags'], ['v*'])
        self.assertIn('workflow_dispatch', self.workflow['on'])
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        release = self.jobs['release']
        self.assertEqual(release['needs'], 'windows')
        self.assertEqual(release['permissions'], {'contents': 'write'})
        self.assertIn("github.event_name == 'push'", release['if'])
        self.assertIn("'refs/tags/v'", release['if'])
        self.assertEqual(self.workflow['concurrency']['cancel-in-progress'], 'false')

    def test_version_validation(self):
        step = next(s for s in self.jobs['windows']['steps'] if s.get('id') == 'version')
        pattern = re.search("-cnotmatch '([^']+)'", step['run']).group(1)
        for value in ('0.3.0', '1.2.3.4', '0.10.0'):
            self.assertRegex(value, pattern)
        for value in ('', 'v0.3.0', '1.2', '1.2.3-rc1', '../1.2.3', '1.2.3;echo bad'):
            self.assertIsNone(re.fullmatch(pattern, value))
        self.assertIn('GITHUB_REF_NAME.Substring(1)', step['run'])
        self.assertIn('GITHUB_OUTPUT', step['run'])

    def test_initial_version_defaults_are_consistent(self):
        version = '0.1.0'
        self.assertEqual(self.workflow['on']['workflow_dispatch']['inputs']['version']['default'], version)
        builder = (ROOT / 'desktop/build_windows.py').read_text(encoding='utf-8')
        installer = (ROOT / 'desktop/installer.iss').read_text(encoding='utf-8')
        self.assertIn(f'parser.add_argument("--version", default="{version}")', builder)
        self.assertIn(f'#define AppVersion "{version}"', installer)

    def test_build_order_and_checksums(self):
        steps = self.jobs['windows']['steps']
        names = [s.get('name', s.get('uses')) for s in steps]
        self.assertLess(names.index('Smoke-test frozen resources and service lifecycle'), names.index('Compile installer'))
        self.assertLess(names.index('Download verified WebView2 bootstrapper'), names.index('Compile installer'))
        compile_step = next(s for s in steps if s.get('name') == 'Compile installer')
        self.assertIn('-Algorithm SHA256', compile_step['run'])
        self.assertIn('-Encoding ascii', compile_step['run'])
        upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@'))
        download = self.jobs['release']['steps'][0]
        self.assertEqual(upload['with']['name'], download['with']['name'])

    def test_draft_upload_publish_and_rerun_guard(self):
        script = self.jobs['release']['steps'][-1]['run']
        self.assertLess(script.index('sha256sum --check'), script.index('gh release create'))
        self.assertLess(script.index("'.isDraft == true'"), script.index('gh release upload'))
        self.assertLess(script.index('gh release upload'), script.index('gh release edit'))
        self.assertIn('--verify-tag --draft', script)
        self.assertIn('--draft=false', script)
        self.assertIn('CreatorHub-Setup-$APP_VERSION-windows-x64.exe', script)

    def test_runtime_is_verified_and_required(self):
        script = (ROOT / 'desktop/prepare_webview2.ps1').read_text(encoding='utf-8')
        installer = (ROOT / 'desktop/installer.iss').read_text(encoding='utf-8')
        self.assertIn('Get-AuthenticodeSignature', script)
        self.assertIn('Microsoft Corporation', script)
        self.assertIn("$signature.Status -ne 'Valid'", script)
        self.assertIn('Flags: dontcopy', installer)
        self.assertIn('function PrepareToInstall', installer)
        self.assertIn('if HasWebView2() then Exit', installer)
        self.assertIn("'/silent /install'", installer)
        self.assertIn('HKLM32', installer)
        self.assertIn('HKCU', installer)

    def test_user_download_entries(self):
        for name in ('README.md', 'desktop/README.md', 'guide/index.html'):
            self.assertIn('https://github.com/3441293738/creatorhub/releases/latest',
                          (ROOT / name).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
