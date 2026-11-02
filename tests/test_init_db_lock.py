"""init_db must not die when another writer holds the lock (2026-11-02)."""
import sqlite3

from directory import db, paid_verify


def test_init_db_survives_locked_housekeeping(tmp_path, monkeypatch):
    path = str(tmp_path / "d.db")
    db.init_db(path)

    def locked(_conn, now=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(paid_verify, "expire_stale", locked)
    db.init_db(path)  # must not raise


def test_init_db_still_raises_other_errors(tmp_path, monkeypatch):
    path = str(tmp_path / "d.db")
    db.init_db(path)

    def broken(_conn, now=None):
        raise sqlite3.OperationalError("no such table: paid_verifications")

    monkeypatch.setattr(paid_verify, "expire_stale", broken)
    try:
        db.init_db(path)
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)
    else:
        raise AssertionError("schema errors must still surface")
