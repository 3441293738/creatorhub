"""Release preparation uses mock Git or an isolated bare repository, never GitHub."""
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from desktop import release


class ReleaseHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notes = "# 更新日志\n\n## [0.2.0]\n\n- 中文更新说明。\n\n## [0.1.0]\n\n- 首版。\n"
        (self.root / "CHANGELOG.md").write_text(self.notes, encoding="utf-8")
        self.calls = []
        self.dirty = ""
        self.head = "a" * 40
        self.local_tag = ""
        self.remote = f"{'b' * 40}\trefs/tags/v0.1.0\n"

    def git(self, *args):
        self.calls.append(args)
        if args[0] == "status":
            return self.dirty
        if args[:2] == ("rev-parse", "HEAD"):
            return self.head
        if args[0] == "rev-parse":
            return self.local_tag
        if args[:2] == ("tag", "--list"):
            return "v0.2.0" if self.local_tag else ""
        if args[0] == "ls-remote":
            return self.remote
        return ""

    def test_notes_are_exactly_one_version_and_numeric_version_is_canonical(self):
        notes = release.release_notes(self.root, "0.2.0")
        self.assertIn("中文更新说明", notes)
        self.assertNotIn("首版", notes)
        self.assertEqual(release.version_key("0.2.0"), release.version_key("0.2.0.0"))
        for version in ("0.2", "v0.2.0", "0.2.0-rc1", "0.02.0", "0.2.0;cmd", "1.2.3.4.5", "9" * 30):
            with self.assertRaises(ValueError):
                release.version_key(version)
        for text in ("## [0.1.0]\nold", "## [0.2.0]\n", self.notes + "\n## [0.2.0]\nduplicate"):
            (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                release.release_notes(self.root, "0.2.0")

    def test_plan_reads_only_and_check_or_cancel_does_not_publish(self):
        with patch.object(release, "ROOT", self.root), patch.object(release, "git_runner", return_value=self.git), \
             contextlib.redirect_stdout(io.StringIO()):
            with patch("builtins.input", side_effect=AssertionError("check must not prompt")):
                self.assertEqual(release.main(["0.2.0", "--check"]), 0)
            with patch("builtins.input", return_value=""):
                self.assertEqual(release.main(["0.2.0"]), 0)
        self.assertFalse(any(args[0] == "push" or args[:2] == ("tag", "-a") for args in self.calls))

    def test_dirty_duplicate_older_or_conflicting_tag_blocks_publication(self):
        for dirty, local, remote in ((" M app.py", "", self.remote),
                ("", "b" * 40, self.remote), ("", "", f"{'a' * 40}\trefs/tags/v0.2.0^{{}}"),
                ("", "", f"{'a' * 40}\trefs/tags/v0.3.0")):
            self.dirty, self.local_tag, self.remote = dirty, local, remote
            with self.assertRaises(ValueError):
                release.plan_release(self.root, "0.2.0", self.git)
        self.assertFalse(any(args[0] == "push" for args in self.calls))

    def test_confirmation_pins_head_and_pushes_only_that_tag(self):
        plan = release.plan_release(self.root, "0.2.0", self.git)
        release.publish_plan(self.root, plan, self.git)
        self.assertIn(("tag", "-a", "v0.2.0", self.head, "-m", "发布 CreatorHub v0.2.0"), self.calls)
        self.assertEqual(self.calls[-1], ("push", "origin", "refs/tags/v0.2.0:refs/tags/v0.2.0"))
        self.assertFalse(any("--force" in args or "--all" in args for args in self.calls))

    def test_changed_head_after_confirmation_is_rejected(self):
        plan = release.plan_release(self.root, "0.2.0", self.git)
        self.head = "c" * 40
        with self.assertRaises(ValueError):
            release.publish_plan(self.root, plan, self.git)
        self.assertFalse(any(args[0] == "push" for args in self.calls))

    def test_existing_same_commit_local_tag_can_be_retried_without_retagging(self):
        self.local_tag = self.head
        plan = release.plan_release(self.root, "0.2.0", self.git)
        release.publish_plan(self.root, plan, self.git)
        self.assertFalse(any(args[:2] == ("tag", "-a") for args in self.calls))
        self.assertEqual(self.calls[-1][0], "push")

    def test_ci_notes_mode_has_no_git_and_does_not_overwrite_source(self):
        with patch.object(release, "ROOT", self.root), contextlib.redirect_stdout(io.StringIO()), \
             patch.object(release, "git_runner", side_effect=AssertionError("CI notes are offline")):
            self.assertEqual(release.main(["0.2.0", "--notes-output", "dist/installer/RELEASE_NOTES.md"]), 0)
            self.assertEqual((self.root / "dist/installer/RELEASE_NOTES.md").read_text(encoding="utf-8"),
                             release.release_notes(self.root, "0.2.0"))
            self.assertEqual(release.main(["0.2.0", "--notes-output", "CHANGELOG.md"]), 1)
            self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), self.notes)

    def test_real_git_push_is_confined_to_disposable_local_remote(self):
        executable = release.git_executable()
        source, remote = self.root / "source", self.root / "remote.git"
        source.mkdir()
        def git_at(path, *args):
            return subprocess.run([executable, *args], cwd=path, check=True, capture_output=True,
                                  text=True, encoding="utf-8", timeout=20).stdout.strip()
        git_at(self.root, "init", "--bare", str(remote))
        git_at(source, "init")
        (source / "CHANGELOG.md").write_text(self.notes, encoding="utf-8")
        git_at(source, "add", "CHANGELOG.md")
        git_at(source, "-c", "user.name=Release Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
        # The annotated tag uses only this temporary repository's identity.
        git_at(source, "config", "user.name", "Release Test")
        git_at(source, "config", "user.email", "test@example.invalid")
        git_at(source, "remote", "add", "origin", str(remote))
        runner = release.git_runner(source)
        plan = release.plan_release(source, "0.2.0", runner)
        release.publish_plan(source, plan, runner)
        self.assertEqual(git_at(remote, "rev-parse", "refs/tags/v0.2.0^{commit}"), plan["head"])
        self.assertEqual(git_at(remote, "for-each-ref", "--format=%(refname)", "refs/heads"), "")
        with self.assertRaises(ValueError):
            release.plan_release(source, "0.2.0", runner)


if __name__ == "__main__":
    unittest.main()
