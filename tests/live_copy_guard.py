"""Guards for script tests that run against a copy of the live database.

directory.db binds AGENT_TOOLS_DB_PATH into module constants at import, so a
script's override only works if nothing imported directory.db before it.
"""
import atexit
import importlib
import os
import sys


def refuse_if_imported():
    if "directory.db" in sys.modules:
        raise SystemExit("ABORT: directory.db is already imported, so AGENT_TOOLS_DB_PATH can no longer "
                         "redirect writes to a copy; run this script on its own: python %s" % sys.argv[0])


def assert_on_copy(copy_path):
    db = sys.modules.get("directory.db") or importlib.import_module("directory.db")
    stem = os.path.splitext(copy_path)[0]
    paths = [base + suffix for base in (copy_path, stem + "-rate-limits.db")
             for suffix in ("", "-wal", "-shm", "-journal", ".lock")]

    def cleanup():
        for path in paths:
            if os.path.exists(path):
                os.remove(path)

    if os.path.realpath(db.DEFAULT_DB_PATH) != os.path.realpath(copy_path):
        cleanup()
        raise SystemExit("ABORT: directory.db writes to %s, not the copy %s" % (db.DEFAULT_DB_PATH, copy_path))
    atexit.register(cleanup)
