from pathlib import Path
import configparser
import tempfile
import unittest

from desktop.check_installer_language import validate_messages


class InstallerLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / "Default.isl"
        self.translated = self.root / "ChineseSimplified.isl"
        self.original.write_text('[Messages]\nButtonNext=Next\nSelectDir=Install [name] to %1\nDiskSpace=Needs [mb] MB\n', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_translation(self):
        self.translated.write_text('[Messages]\nButtonNext=下一步\nSelectDir=将 [name] 安装到 %1\nDiskSpace=需要 [mb] MB\n', encoding='utf-8')
        self.assertEqual(validate_messages(self.original, self.translated), 3)

    def test_new_compiler_message_requires_translation(self):
        self.translated.write_text('[Messages]\nButtonNext=下一步\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Missing Chinese installer messages'):
            validate_messages(self.original, self.translated)

    def test_placeholder_changes_are_rejected(self):
        self.translated.write_text('[Messages]\nButtonNext=下一步\nSelectDir=安装到 %2\nDiskSpace=需要 [gb] GB\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Changed installer message placeholders'):
            validate_messages(self.original, self.translated)

    def test_duplicate_translation_keys_are_rejected(self):
        self.translated.write_text('[Messages]\nButtonNext=下一步\nButtonNext=Next\n', encoding='utf-8')
        with self.assertRaises(configparser.DuplicateOptionError):
            validate_messages(self.original, self.translated)


if __name__ == '__main__':
    unittest.main()
