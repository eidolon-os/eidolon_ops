import sqlite3

import pytest

from eidolon_ops.errors import OperationsError
from eidolon_ops.smarthome_state import pending, transfer


def test_transfer_preserves_wal_rows_schema_and_original(tmp_path):
    source = tmp_path / "channel/smarthome.sqlite3"
    source.parent.mkdir()
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA user_version=7")
        db.execute("CREATE TABLE owned_state (owner TEXT, value TEXT)")
        db.execute("INSERT INTO owned_state VALUES ('owner-a', 'locked')")
        db.commit()
        assert pending(tmp_path)
        transfer(tmp_path)
        assert db.execute("SELECT * FROM owned_state").fetchall() == [("owner-a", "locked")]
    target = tmp_path / "hub/smarthome.sqlite3"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not pending(tmp_path)
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (7,)
        assert db.execute("SELECT * FROM owned_state").fetchall() == [("owner-a", "locked")]
    with pytest.raises(OperationsError, match="already exists"):
        transfer(tmp_path)


def test_symlink_source_refused(tmp_path):
    (tmp_path / "channel").mkdir()
    other = tmp_path / "other"
    other.touch()
    (tmp_path / "channel/smarthome.sqlite3").symlink_to(other)
    with pytest.raises(OperationsError, match="regular source"):
        transfer(tmp_path)
