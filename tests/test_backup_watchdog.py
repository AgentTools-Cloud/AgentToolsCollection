"""Stdlib-only watchdog checks: AST extraction never imports production code."""
import ast
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "ops/job_health_alert.py"
ARCHIVE = "agent-tools-20260922T000000000000Z-0123456789abcdef.tar.gz.age"


class BackupWatchdogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.receipt = Path(tmp.name) / "last-success.json"
        self.now = 1_790_100_000
        self.uid = 0
        def fstat(fd):
            info = os.fstat(fd)
            return types.SimpleNamespace(st_mode=info.st_mode, st_uid=self.uid)
        safe_os = types.SimpleNamespace(open=os.open, fdopen=os.fdopen, fstat=fstat,
                                       O_RDONLY=os.O_RDONLY, O_NOFOLLOW=os.O_NOFOLLOW, O_NONBLOCK=os.O_NONBLOCK)
        self.proc = types.SimpleNamespace(run=Mock(side_effect=AssertionError("unexpected subprocess")))
        self.ns = {"json": json, "os": safe_os, "stat": stat, "re": re, "subprocess": self.proc,
                   "time": types.SimpleNamespace(time=lambda: self.now), "BACKUP_SUCCESS": self.receipt}
        tree = ast.parse(SOURCE.read_text())
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name in {"backup_is_fresh", "unit_results"}]
        self.assertEqual(len(selected), 2)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), self.ns)

    def write_receipt(self, **changes):
        state = {"version": 1, "archive": ARCHIVE, "finished_at": self.now - 60, "offsite": True}
        state.update(changes)
        self.receipt.write_text(json.dumps(state))
        self.receipt.chmod(0o600)

    def fresh(self):
        return self.ns["backup_is_fresh"]()

    def test_fresh_receipt_needs_no_systemd_timestamp_or_subprocess(self):
        self.write_receipt()
        self.assertTrue(self.fresh())
        self.proc.run.assert_not_called()

    def test_30_hour_boundary_stale_and_future(self):
        for age, expected in ((0, True), (30 * 3600, True), (30 * 3600 + 1, False), (-1, False)):
            with self.subTest(age=age):
                self.write_receipt(finished_at=self.now - age)
                self.assertIs(self.fresh(), expected)

    def test_missing_corrupt_or_local_only_is_unhealthy(self):
        self.assertFalse(self.fresh())
        for text in ("broken", "null", "[]", "{}", "{", '"string"'):
            self.receipt.write_text(text)
            self.receipt.chmod(0o600)
            self.assertFalse(self.fresh())
        for changes in ({"version": 2}, {"version": True}, {"offsite": False}, {"offsite": 1}, {"archive": "../bad"},
                        {"archive": None}, {"finished_at": "yesterday"}, {"finished_at": True},
                        {"finished_at": None}, {"finished_at": 0}, {"finished_at": float("nan")},
                {"finished_at": float("inf")}, {"finished_at": float("-inf")},
                {"finished_at": 10 ** 1000}):
            with self.subTest(changes=changes):
                self.write_receipt(**changes)
                self.assertFalse(self.fresh())

    def test_oversized_deeply_nested_and_symlink_loop_fail_closed(self):
        self.write_receipt()
        with self.receipt.open("a") as stream:
            stream.write(" " * 4096)
        self.assertFalse(self.fresh())
        self.receipt.write_text("[" * 1500 + "]" * 1500)
        self.assertFalse(self.fresh())
        self.receipt.unlink()
        self.receipt.symlink_to(self.receipt)
        self.assertFalse(self.fresh())

    def test_receipt_requires_root_private_regular_non_symlink(self):
        self.write_receipt()
        self.uid = 1
        self.assertFalse(self.fresh())
        self.uid = 0
        for mode in (0o644, 0o640, 0o666, 0o400):
            self.receipt.chmod(mode)
            self.assertFalse(self.fresh())
        self.receipt.chmod(0o600)
        actual = self.receipt.with_name("actual.json")
        self.receipt.rename(actual)
        self.receipt.symlink_to(actual)
        self.assertFalse(self.fresh())
        self.receipt.unlink()
        self.receipt.mkdir()
        self.assertFalse(self.fresh())
        self.receipt.rmdir()
        os.mkfifo(self.receipt, 0o600)
        self.assertFalse(self.fresh())

    def test_unloaded_units_and_reboot_still_use_durable_receipt(self):
        self.write_receipt()
        self.proc.run.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="")
        self.assertEqual(self.ns["unit_results"](), [])
        self.assertTrue(self.fresh())
        self.now += 31 * 3600
        self.assertFalse(self.fresh())
        self.assertNotIn("ExecMainExitTimestamp", SOURCE.read_text())

    def test_exact_backup_unit_failure_is_not_hidden_by_fresh_receipt(self):
        self.write_receipt()
        def result(args, **kwargs):
            self.assertEqual(args[:2], ["systemctl", "show"])
            self.assertEqual(args[3:], ["-p", "Result", "--value"])
            value = "exit-code" if args[2] == "agent-tools-backup.service" else "success"
            return subprocess.CompletedProcess(args, 0, stdout=value)
        self.proc.run.side_effect = result
        self.assertTrue(self.fresh())
        self.assertEqual(self.ns["unit_results"](), [("backup", "exit-code")])

    def test_main_calls_both_unit_result_and_receipt_checks(self):
        main = next(node for node in ast.parse(SOURCE.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = {node.func.id for node in ast.walk(main) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertTrue({"unit_results", "backup_is_fresh"} <= calls)


if __name__ == "__main__":
    unittest.main()