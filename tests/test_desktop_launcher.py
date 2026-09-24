from pathlib import Path
import io
import os
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

from desktop.launcher import (InstanceLock, bind_local_port, child_command,
                              configure_process_streams, prepare_home, snapshot,
                              user_directory, utf8_child_environment)


class DesktopTests(unittest.TestCase):
    def test_custom_home_is_explicit_and_separate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREATORHUB_DESKTOP_HOME": tmp}):
            self.assertEqual(user_directory(), Path(tmp).resolve())

    def test_prepare_preserves_config_and_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prepare_home(home)
            (home / "config.yaml").write_text("custom: true", encoding="utf-8")
            (home / "private.db").write_bytes(b"existing data")
            prepare_home(home)
            self.assertEqual((home / "config.yaml").read_text(), "custom: true")
            self.assertEqual((home / "private.db").read_bytes(), b"existing data")

    def test_snapshot_contains_consistent_database_and_no_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prepare_home(home)
            (home / "config.yaml").write_text("storage:\n  db_path: custom.db\n", encoding="utf-8")
            with closing(sqlite3.connect(home / "custom.db")) as db:
                db.execute("create table example (value text)")
                db.execute("insert into example values ('keep me')")
                db.commit()
            (home / "logs" / "private.txt").write_text("SECRET")
            with zipfile.ZipFile(snapshot(home)) as archive:
                self.assertEqual(set(archive.namelist()), {"config.yaml", "database.db", "README.txt"})
                archive.extract("database.db", home / "restore")
            with closing(sqlite3.connect(home / "restore" / "database.db")) as db:
                self.assertEqual(db.execute("select value from example").fetchone()[0], "keep me")

    def test_port_falls_back_without_reusing_another_server(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with bind_local_port(occupied.getsockname()[1]) as chosen:
                self.assertNotEqual(chosen.getsockname()[1], occupied.getsockname()[1])
                self.assertEqual(chosen.getsockname()[0], "127.0.0.1")

    def test_frozen_command_is_not_python_module_invocation(self):
        with patch("sys.frozen", True, create=True), patch("sys.executable", "CreatorHub.exe"):
            self.assertEqual(child_command("--serve"), ["CreatorHub.exe", "--serve"])

    def test_child_environment_overrides_legacy_windows_encoding(self):
        with patch.dict(os.environ, {"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"}):
            environment = utf8_child_environment(CREATORHUB_DESKTOP_HOME="C:/CreatorHub")
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertEqual(environment["CREATORHUB_DESKTOP_HOME"], "C:/CreatorHub")

    def test_redirected_cp1252_stream_is_reconfigured_for_chinese_startup_message(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        try:
            with patch("sys.stdout", stream), patch("sys.stderr", stream):
                configure_process_streams(Path("unused"))
                print("首次启动：下载浏览器组件，请保持联网。完成后会自动打开面板。", flush=True)
            self.assertEqual(stream.encoding.lower(), "utf-8")
            self.assertIn("首次启动", raw.getvalue().decode("utf-8"))
        finally:
            stream.detach()

    @unittest.skipUnless(os.name == "nt", "Windows locking")
    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prepare_home(home)
            first = InstanceLock(home)
            try:
                with self.assertRaises(RuntimeError):
                    InstanceLock(home)
            finally:
                first.close()
            second = InstanceLock(home)
            second.close()


if __name__ == "__main__":
    unittest.main()
