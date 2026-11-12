"""Schema caching must retain disk persistence and per-test data isolation."""
import shutil
import sqlite3
from contextlib import closing
from unittest.mock import patch

import app.db as db
from test_project_optimizations import empty_project_schema


def test_schema_is_initialized_once_and_never_leaves_its_engine_active(tmp_path):
    previous = db._engine
    with patch.object(db, "init_db", wraps=db.init_db) as initialize:
        template = empty_project_schema(tmp_path)
        assert empty_project_schema(tmp_path) == template
    initialize.assert_called_once_with(str(template))
    assert db._engine is previous
    with closing(sqlite3.connect(template)) as connection:
        for name in ("douyinaccount", "monitortarget", "publishtask"):
            assert connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] == 0
    # A retained connection would make this rename fail on Windows.
    renamed = tmp_path / "closed-template.db"
    template.rename(renamed)


def test_copies_are_independent_real_databases_with_normal_durability(tmp_path):
    template = empty_project_schema(tmp_path)
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    for path in (first, second):
        shutil.copyfile(template, path)
    with closing(sqlite3.connect(first)) as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        connection.execute("CREATE TABLE fixture_marker (value TEXT)")
        connection.execute("INSERT INTO fixture_marker VALUES ('persisted')")
        connection.commit()
    with closing(sqlite3.connect(first)) as reopened:
        assert reopened.execute("SELECT value FROM fixture_marker").fetchone()[0] == "persisted"
    for path in (template, second):
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='fixture_marker'").fetchone() is None
