"""Release wiring checks; no network, credentials, or real publication."""
from pathlib import Path
import re
import unittest
import configparser

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

    def test_chinese_notes_are_validated_before_build_and_used_for_release(self):
        from desktop.release import release_notes
        steps = self.jobs['windows']['steps']
        names = [s.get('name', s.get('uses')) for s in steps]
        self.assertLess(names.index('Prepare Chinese release notes'), names.index('Build desktop renderer'))
        notes = next(s for s in steps if s.get('name') == 'Prepare Chinese release notes')
        self.assertIn('--notes-output dist/installer/RELEASE_NOTES.md', notes['run'])
        self.assertIn('Test release helper', names)
        script = self.jobs['release']['steps'][-1]['run']
        self.assertIn('--notes-file RELEASE_NOTES.md', script)
        self.assertNotIn('--generate-notes', script)
        self.assertIn('每天最多一次', release_notes(ROOT, '0.2.0'))

    def test_initial_version_defaults_are_consistent(self):
        version = '0.2.0'
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
        self.assertIn('desktop/check_installer_language.py --compiler', compile_step['run'])
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

    def test_independent_updater_is_packaged_and_smoke_tested(self):
        builder = (ROOT / 'desktop/build_windows.py').read_text(encoding='utf-8')
        self.assertIn('"--onefile"', builder)
        self.assertIn('"CreatorHubUpdater"', builder)
        self.assertIn('(helper, "desktop")', builder)
        self.assertIn('"--self-test"', builder)
        steps = self.jobs['windows']['steps']
        smoke = next(step for step in steps if step.get('name') == 'Test frozen updater handoff with disposable fixtures')
        self.assertIn('CreatorHub/_internal/desktop/CreatorHubUpdater.exe', smoke['run'])

    def test_installer_accepts_an_explicit_build_directory(self):
        installer = (ROOT / 'desktop/installer.iss').read_text(encoding='utf-8')
        self.assertIn('#ifndef AppSourceDir', installer)
        self.assertIn('Source: "{#AppSourceDir}\\*"', installer)
        self.assertIn('#define AppSourceDir "..\\dist\\windows\\CreatorHub"', installer)

    def test_delta_inventory_is_built_after_setup_and_published_with_checksums(self):
        steps = self.jobs['windows']['steps']
        names = [s.get('name', s.get('uses')) for s in steps]
        self.assertLess(names.index('Compile installer'), names.index('Build file delta and release inventory'))
        self.assertLess(names.index('Fetch verified previous release inventory'), names.index('Build file delta and release inventory'))
        self.assertIn('Test frozen file delta and automatic rollback', names)
        script = self.jobs['release']['steps'][-1]['run']
        self.assertIn('CreatorHub-Update-$APP_VERSION-windows-x64.json', script)
        self.assertIn('CreatorHub-Delta-*-to-', script)
        self.assertIn('"${assets[@]}"', script)
        dependencies = next(step for step in steps if step.get('name') == 'Install build dependencies')
        self.assertIn('-c desktop/constraints-windows.txt', dependencies['run'])

    def test_installer_defaults_to_vendored_simplified_chinese(self):
        installer = (ROOT / 'desktop/installer.iss').read_text(encoding='utf-8')
        languages = installer.split('[Languages]', 1)[1].split('[LangOptions]', 1)[0]
        self.assertIn('Name: "chinesesimplified"; MessagesFile: "languages\\ChineseSimplified.isl"', languages)
        self.assertEqual(len(re.findall(r'^Name:', languages, re.M)), 1)
        self.assertIn('ShowLanguageDialog=no', installer)
        self.assertIn('UsePreviousLanguage=no', installer)
        self.assertIn('AppVerName=CreatorHub v{#AppVersion}', installer)
        self.assertNotIn('compiler:Languages', languages)
        self.assertNotIn('Default.isl', languages)
        self.assertIn('DialogFontName=Microsoft YaHei UI', installer)

    def test_chinese_messages_cover_wizard_buttons_errors_and_uninstall(self):
        messages = configparser.ConfigParser(interpolation=None)
        messages.optionxform = str
        messages.read(ROOT / 'desktop/languages/ChineseSimplified.isl', encoding='utf-8-sig')
        self.assertEqual(messages['LangOptions']['LanguageID'], '$0804')
        for key in ('SetupWindowTitle', 'ButtonNext', 'ButtonBack', 'ButtonCancel', 'ButtonInstall',
                    'ButtonFinish', 'ButtonWizardBrowse', 'WizardSelectDir', 'SelectDirDesc',
                    'SelectDirBrowseLabel', 'DiskSpaceMBLabel', 'WizardSelectTasks', 'WizardReady',
                    'WizardPreparing', 'WizardInstalling', 'FinishedHeadingLabel',
                    'ConfirmUninstall', 'UninstallStatusLabel', 'SetupFileCorrupt'):
            with self.subTest(key=key):
                self.assertRegex(messages['Messages'][key], r'[\u4e00-\u9fff]')
        self.assertIn('[name]', messages['Messages']['SelectDirDesc'])
        self.assertIn('[mb]', messages['Messages']['DiskSpaceMBLabel'])
        self.assertIn('%1', messages['Messages']['SetupWindowTitle'])

    def test_custom_installer_messages_and_translation_license_are_wired(self):
        installer = (ROOT / 'desktop/installer.iss').read_text(encoding='utf-8')
        for key in ('InstallingWebView2', 'WebView2StartFailed', 'WebView2Required', 'UninstallKeepData'):
            self.assertIn(f"CustomMessage('{key}')", installer)
            value = re.search(rf'^{key}=(.+)$', installer, re.M).group(1)
            self.assertRegex(value, r'[\u4e00-\u9fff]')
        self.assertIn('Description: "{cm:CreateDesktopIcon}"', installer)
        self.assertIn('Description: "{cm:LaunchCreatorHub}"', installer)
        self.assertNotIn('Description: "Create a desktop shortcut"', installer)
        self.assertNotIn('Description: "Launch CreatorHub"', installer)
        license_path = ROOT / 'desktop/languages/ChineseSimplified-LICENSE.txt'
        self.assertIn('MIT License', license_path.read_text(encoding='utf-8'))
        self.assertIn('ChineseSimplified-LICENSE.txt', (ROOT / 'desktop/build_windows.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
