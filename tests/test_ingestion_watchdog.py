"""Offline watchdog regressions; never import directory or the production entrypoint.

Only selected AST definitions/constants are evaluated. SQLite reads use a
read-only fixture connection, time is frozen, and journal/mail/state are mocks.
"""

import ast
from collections import Counter
from contextlib import closing, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "ops/job_health_alert.py"
NOW = 1_792_000_000
HOUR = 3600


class IngestionWatchdogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ingestion-watchdog-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "fixture.db"
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE crawl_runs (
                id INTEGER PRIMARY KEY, source TEXT, started_at INTEGER,
                finished_at INTEGER, added INTEGER, updated INTEGER,
                errors TEXT, status TEXT NOT NULL
            );
            CREATE TABLE mcp_servers (updated_at INTEGER);
            CREATE TABLE services (updated_at INTEGER);
            CREATE TABLE a2a_agents (updated_at INTEGER);
            CREATE TABLE ingestion_issues (
                kind TEXT NOT NULL, source TEXT NOT NULL,
                identity_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
                disposition TEXT NOT NULL, reason TEXT NOT NULL,
                first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1, resolved_at INTEGER,
                PRIMARY KEY(kind, source, identity_hash, content_hash, reason)
            );
            -- An unrelated ingestion observation table must be harmless.
            CREATE TABLE ingestion_observations (payload TEXT);
            INSERT INTO ingestion_observations VALUES ('MUST_NOT_APPEAR');
            CREATE TABLE source_endpoint_warnings (warning_key TEXT);
        """)
        for table in ("mcp_servers", "services", "a2a_agents"):
            self.conn.execute(f"INSERT INTO {table} VALUES (?)", (NOW - 10,))
        self.conn.commit()
        self.now = NOW
        self.state = {}
        self.clock = Mock(side_effect=lambda: self.now)
        clock = self.clock

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(clock(), tz)

        def connect(*, read_only):
            self.assertIs(read_only, True)
            return sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)

        self.warning_rows = []
        self.db = types.SimpleNamespace(connect=Mock(side_effect=connect),
                        visible_source_endpoint_warnings=Mock(side_effect=lambda conn: self.warning_rows))
        self.mail = Mock(return_value=True)
        self.save = Mock(side_effect=lambda state: setattr(self, "state", deepcopy(state)))
        forbidden = Mock(side_effect=AssertionError("external I/O is forbidden"))
        self.ns = {
            "Counter": Counter, "closing": closing, "datetime": FrozenDatetime,
            "timezone": timezone, "json": json, "re": re,
            "time": types.SimpleNamespace(time=self.clock), "db": self.db,
            "journal": Mock(return_value=[
                "directory.jobs health: ok=1", "directory.jobs mcp health: ok=1",
                "directory.jobs a2a health: ok=1",
            ]),
            "unit_results": Mock(return_value=[]), "backup_is_fresh": Mock(return_value=True),
            "load_state": Mock(side_effect=lambda: deepcopy(self.state)), "save_state": self.save,
            "mailer": types.SimpleNamespace(send_internal_notice=self.mail),
            "subprocess": types.SimpleNamespace(run=forbidden),
        }
        functions = {"db_checks", "latest_completed_runs", "failing_sources", "stuck_sources",
                     "ingestion_checks", "source_warning_checks", "main"}
        constants = {"WINDOW_SECONDS", "STALE_HOURS", "FRESH_TABLES", "FRESH_SOURCES",
                     "ABORT_RE", "DONE_RE", "MIN_PASSES", "PARTIAL_STREAK"}
        selected = []
        for node in ast.parse(SOURCE.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name in functions:
                selected.append(node)
            elif isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in constants for target in node.targets):
                selected.append(node)
        self.assertEqual({node.name for node in selected if isinstance(node, ast.FunctionDef)}, functions)
        self.assertEqual(len(selected), len(functions) + len(constants))
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), self.ns)

    def run_row(self, source="ordinary", status="partial", age=60, errors=None,
                added=10, updated=0, row_id=None, finished=True):
        ts = self.now - age
        self.conn.execute(
            "INSERT INTO crawl_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, source, ts, ts if finished else None, added, updated, errors, status),
        )
        self.conn.commit()

    def issue(self, source="agenstry", kind="a2a", reason="invalid_endpoint",
              disposition="rejected", identity="PRIVATE_IDENTITY_HASH", content="PRIVATE_CONTENT_HASH",
              age=60, last_age=10, occurrences=1, resolved_at=None):
        self.conn.execute(
            "INSERT INTO ingestion_issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, source, identity, content, disposition, reason,
             self.now - age, self.now - last_age, occurrences, resolved_at),
        )
        self.conn.commit()

    def partials(self, source="ordinary", count=4):
        for index in range(count):
            self.run_row(source, age=(count - index) * HOUR,
                         errors=f"error-{index}\nsecond-{index}")

    def main(self):
        self.mail.reset_mock()
        self.save.reset_mock()
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.ns["main"]()
        text = self.mail.call_args.args[2] if self.mail.called else output.getvalue()
        return result, text

    def test_running_does_not_hide_latest_error(self):
        self.run_row(status="error", age=100)
        self.run_row(status="running", age=0, finished=False)
        self.assertEqual(self.ns["failing_sources"](), ["ordinary"])

    def test_latest_status_uses_timestamp_then_id_with_legacy_completion_fallback(self):
        self.run_row(source="recovered", status="error", row_id=20)
        self.run_row(source="recovered", status="ok", row_id=21, finished=False)
        self.run_row(source="failed", status="error", row_id=31)
        self.run_row(source="failed", status="ok", row_id=30)
        self.run_row(source="timestamp", status="error", age=10, row_id=2)
        self.run_row(source="timestamp", status="ok", age=100, row_id=40)
        self.assertEqual(self.ns["failing_sources"](), ["failed", "timestamp"])

    def test_error_window_excludes_old_future_and_running(self):
        self.run_row(source="old", status="error", age=24 * HOUR + 1)
        self.run_row(source="future", status="error", age=-1)
        self.run_row(source="running", status="running", finished=False)
        self.run_row(source="boundary", status="error", age=24 * HOUR)
        self.assertEqual(self.ns["failing_sources"](), ["boundary"])

    def test_partial_tuple_counts_runs_not_hours_and_uses_latest_error(self):
        self.partials()
        self.run_row(status="running", age=0, errors="running error", finished=False)
        self.assertEqual(self.ns["stuck_sources"](), [("ordinary", 4, "error-3")])
        result, text = self.main()
        self.assertEqual(result, 1)
        self.assertIn("连续 4 轮已完成运行均为 partial", text)
        self.assertIn("最新 partial 轮次的首条报错: error-3", text)
        self.assertNotIn("error-0", text)
        self.assertNotIn("96h", text)
        self.assertNotIn("没跑全", text)

    def test_partial_order_is_stable_and_completed_ok_breaks_streak(self):
        self.partials()
        self.run_row(status="ok", age=HOUR, row_id=10)
        self.run_row(status="partial", age=HOUR, row_id=11)
        self.assertEqual(self.ns["stuck_sources"](), [])

    def test_partial_running_between_completions_does_not_break_streak(self):
        self.partials()
        self.run_row(status="running", age=2 * HOUR, finished=False)
        self.assertEqual(self.ns["stuck_sources"](), [("ordinary", 4, "error-3")])

    def test_partial_uses_latest_empty_or_truncated_error_not_old_error(self):
        self.partials(count=3)
        self.run_row(errors=None, age=1)
        self.assertEqual(self.ns["stuck_sources"](), [("ordinary", 4, "")])
        self.run_row(errors="x" * 200 + "\nprivate second line", age=0)
        self.assertEqual(self.ns["stuck_sources"](), [("ordinary", 5, "x" * 120)])

    def test_partial_threshold_and_window(self):
        self.partials(count=3)
        self.partials(source="stale")
        self.conn.execute("UPDATE crawl_runs SET started_at=started_at-?, finished_at=finished_at-? "
                          "WHERE source='stale'", (24 * HOUR, 24 * HOUR))
        self.conn.commit()
        self.assertEqual(self.ns["stuck_sources"](), [])

    def test_db_write_checks_exclude_running_error_and_future(self):
        for source, status, age in (("running", "running", 0), ("failed", "error", 0),
                                    ("future", "partial", -1)):
            self.run_row(source, status, age, errors="failure", added=0)
        self.assertEqual(self.ns["db_checks"]()[0], [])
        self.run_row("ordinary", errors="failure", added=0)
        self.assertEqual(self.ns["db_checks"]()[0], ["ordinary 跑完但一条未写入，报错 1 条"])

    def test_subsource_freshness_starts_only_after_first_appearance(self):
        self.assertNotIn("a2a:agenstry", "\n".join(sum(self.ns["db_checks"](), [])))
        self.run_row("a2a:agenstry", status="ok", age=30 * HOUR)
        self.assertEqual(self.ns["db_checks"]()[0], [])
        self.now += 1
        problems, _ = self.ns["db_checks"]()
        self.assertEqual(len(problems), 1)
        self.assertIn("a2a:agenstry 最近完成已过 30.0 小时", problems[0])

    def test_stale_child_not_hidden_by_fresh_parent_or_running_child(self):
        self.run_row("a2a:agenstry", status="ok", age=31 * HOUR)
        self.run_row("a2a:agenstry", status="running", age=0, finished=False)
        self.run_row("a2a:crawl", status="ok", age=0)
        self.run_row("agenstry:crawl", status="ok", age=0)
        problems, _ = self.ns["db_checks"]()
        self.assertEqual(len(problems), 1)
        self.assertIn("a2a:agenstry", problems[0])

    def test_first_running_child_has_grace_period_then_becomes_stale(self):
        self.run_row("a2a:agenstry", status="running", age=30 * HOUR, finished=False)
        self.assertEqual(self.ns["db_checks"]()[0], [])
        self.now += 1
        self.assertIn("尚无已完成轮次", self.ns["db_checks"]()[0][0])

    def test_child_partial_immediate_even_with_many_parent_writes_no_repeat_mail(self):
        self.run_row("a2a:agenstry", errors="child latest failure", added=0)
        self.run_row("a2a:crawl", status="ok", added=500)
        self.run_row("agenstry:crawl", status="ok", added=500)
        self.assertEqual(self.ns["db_checks"]()[0], [])
        result, text = self.main()
        self.assertEqual(result, 1)
        self.assertEqual(text.count("- a2a:agenstry"), 1)
        self.assertIn("连续 1 轮已完成运行均为 partial", text)
        self.assertIn("最新 partial 轮次的首条报错: child latest failure", text)
        self.assertEqual(self.main()[0], 0)
        self.mail.assert_not_called()
        self.save.assert_not_called()

    def test_child_error_immediate_even_when_parent_is_successful(self):
        self.run_row("a2a:agenstry", status="error", added=0)
        self.run_row("a2a:crawl", status="ok", added=500)
        result, text = self.main()
        self.assertEqual(result, 1)
        self.assertIn("新增失败爬源: a2a:agenstry", text)
        self.assertNotIn("连续", text)

    def test_first_ever_error_alerts_and_known_error_stays_quiet(self):
        self.run_row(status="error")
        result, text = self.main()
        self.assertEqual(result, 1)
        self.assertIn("新增失败爬源: ordinary", text)
        self.assertEqual(self.state["failing_sources"], ["ordinary"])
        self.assertEqual(self.main()[0], 0)
        self.mail.assert_not_called()
        self.save.assert_not_called()

    def test_absence_partial_error_running_and_unknown_never_prove_recovery(self):
        sources = ["absent", "error", "partial", "running", "unknown"]
        self.state = {"failing_sources": sources[:], "stuck_sources": sources[:]}
        self.run_row("error", status="error", age=48 * HOUR)
        self.run_row("partial", status="partial", age=1)
        self.run_row("running", status="running", age=0, finished=False)
        self.run_row("unknown", status="cancelled", age=0)
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertNotIn("已恢复", text)
        self.assertEqual(self.state["failing_sources"], sources)
        self.assertEqual(self.state["stuck_sources"], sources)

    def test_completed_ok_outside_window_is_recovery_even_with_new_running(self):
        self.state = {"failing_sources": ["fixed"], "stuck_sources": ["fixed"]}
        self.run_row("fixed", status="ok", age=48 * HOUR)
        self.run_row("fixed", status="running", age=0, finished=False)
        # Trigger an unrelated warning so the recovery detail is visible.
        self.run_row("new", status="error")
        _, text = self.main()
        self.assertIn("已恢复的爬源: fixed", text)
        self.assertIn("已恢复的 partial 源: fixed", text)
        self.assertEqual(self.state["failing_sources"], ["new"])
        self.assertEqual(self.state["stuck_sources"], [])

    def test_aged_out_failures_retained_and_return_not_reported_as_new(self):
        self.run_row(status="error")
        self.main()
        self.now += 25 * HOUR
        self.main()
        self.assertEqual(self.state["failing_sources"], ["ordinary"])
        self.run_row(status="error")
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertNotIn("新增失败", text)

    def test_new_rejection_group_warns_aggregate_only_no_payload_or_hashes(self):
        self.issue(occurrences=3)
        self.issue(identity="OTHER_PRIVATE_IDENTITY", occurrences=5, last_age=0)
        self.issue(identity="RESOLVED_PRIVATE_IDENTITY", occurrences=100, resolved_at=NOW)
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertTrue(self.mail.called)
        self.assertIn("新增数据质量拒绝组: a2a:agenstry rejected/invalid_endpoint", text)
        self.assertIn("来源身份 2, 内容版本 2, 累计出现 8", text)
        self.assertIn(datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), text)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("MUST_NOT_APPEAR", text)
        self.assertNotIn("100", text)
        self.assertEqual(len(self.state["ingestion_reason_groups"]), 1)
        self.assertEqual(self.main()[0], 0)

    def test_rejection_repeat_and_new_identity_or_content_are_not_new_reason(self):
        self.issue()
        self.main()
        self.issue(identity="new_identity", content="new_content", occurrences=20)
        self.issue(content="changed_content", occurrences=2)
        self.assertEqual(self.main()[0], 0)
        self.mail.assert_not_called()
        self.issue(reason="hostname_label_too_long")
        _, text = self.main()
        self.assertIn("- 新增数据质量拒绝组: a2a:agenstry rejected/hostname_label_too_long", text)
        self.assertNotIn("- 新增数据质量拒绝组: a2a:agenstry rejected/invalid_endpoint", text)

    def test_old_active_holds_are_detail_only_even_if_recently_seen(self):
        self.issue(age=10 * 24 * HOUR, last_age=0, occurrences=30, disposition="hold")
        problems, detail, holds = self.ns["ingestion_checks"](self.state)
        self.assertEqual(problems, [])
        self.assertIn("来源身份 1, 内容版本 1, 累计出现 30", "\n".join(detail))
        self.assertTrue({"agenstry", "a2a:agenstry", "a2a:crawl", "agenstry:crawl"} <= holds)

    def test_group_first_seen_includes_resolved_history(self):
        self.issue(identity="old", age=7 * 24 * HOUR, resolved_at=NOW - HOUR)
        self.issue(identity="new", age=10)
        problems, detail, _ = self.ns["ingestion_checks"]({})
        self.assertEqual(problems, [])
        self.assertIn("来源身份 1, 内容版本 1, 累计出现 1", "\n".join(detail))

    def test_rejection_group_window_boundary_and_future(self):
        self.issue(reason="boundary", age=24 * HOUR)
        self.issue(reason="old", age=24 * HOUR + 1)
        self.issue(reason="future", age=-1, last_age=-1)
        problems, _, _ = self.ns["ingestion_checks"]({})
        self.assertEqual(len(problems), 1)
        self.assertIn("rejected/boundary", problems[0])

    def test_kind_source_disposition_are_part_of_reason_group(self):
        self.issue()
        self.issue(kind="mcp")
        self.issue(source="other")
        self.issue(disposition="needs_configuration", identity="held_identity")
        problems, _, _ = self.ns["ingestion_checks"]({})
        self.assertEqual(len(problems), 4)

    def test_known_hold_with_ok_child_and_parent_never_announces_recovery(self):
        names = ["a2a:agenstry", "a2a:crawl", "agenstry:crawl"]
        self.state = {"failing_sources": names[:], "stuck_sources": names[:]}
        self.issue(age=48 * HOUR, disposition="hold")
        for source in names:
            self.run_row(source, status="ok")
        self.run_row("unrelated", status="error")
        _, text = self.main()
        self.assertNotIn("已恢复", text)
        self.assertIn("活跃摄入待审", text)
        self.assertEqual(self.state["failing_sources"], names + ["unrelated"])
        self.assertEqual(self.state["stuck_sources"], names)

    def test_other_a2a_child_hold_also_prevents_parent_recovery(self):
        self.state = {"failing_sources": ["a2a:crawl"], "stuck_sources": ["a2a:crawl"]}
        self.issue(source="wellknown", age=48 * HOUR, disposition="hold")
        self.run_row("a2a:crawl", status="ok")
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.state["failing_sources"], ["a2a:crawl"])
        self.assertEqual(self.state["stuck_sources"], ["a2a:crawl"])

    def test_disappearing_issue_group_does_not_announce_recovery_or_forget_observation(self):
        self.issue()
        self.main()
        observed = self.state["ingestion_reason_groups"][:]
        self.conn.execute("DELETE FROM ingestion_issues")
        self.conn.commit()
        self.run_row("unrelated", status="error")
        _, text = self.main()
        self.assertNotIn("已恢复", text)
        self.assertEqual(self.state["ingestion_reason_groups"], observed)
        self.issue()
        self.assertEqual(self.main()[0], 0)

    def test_resolution_alone_does_not_recover_source_without_completed_ok(self):
        self.state = {"failing_sources": ["a2a:agenstry"], "stuck_sources": ["a2a:agenstry"]}
        self.issue(resolved_at=NOW)
        self.run_row("a2a:agenstry", status="partial")
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.state["failing_sources"], ["a2a:agenstry"])
        self.assertEqual(self.state["stuck_sources"], ["a2a:agenstry"])

    def test_resolved_holds_and_completed_ok_allow_recovery(self):
        self.state = {"failing_sources": ["a2a:agenstry"], "stuck_sources": ["a2a:agenstry"]}
        self.issue(resolved_at=NOW)
        self.run_row("a2a:agenstry", status="ok")
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.state["failing_sources"], [])
        self.assertEqual(self.state["stuck_sources"], [])

    def test_missing_issue_table_does_not_crash_or_reset_known_observations(self):
        self.state = {"ingestion_reason_groups": ["previously-observed"]}
        self.conn.execute("DROP TABLE ingestion_issues")
        self.conn.commit()
        self.assertEqual(self.ns["ingestion_checks"](self.state), ([], [], set()))
        self.assertEqual(self.state["ingestion_reason_groups"], ["previously-observed"])
        self.assertEqual(self.main()[0], 0)

    def test_observation_is_read_only_and_no_external_state_or_process_access(self):
        self.issue()
        before = list(self.conn.iterdump())
        self.main()
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertTrue(self.save.called)
        self.assertNotIn("STATE", self.ns)
        self.assertNotIn("sys", self.ns)
        self.assertNotIn("directory", self.ns)
        self.ns["subprocess"].run.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ["fixture.db"])

    def test_content_versions_count_as_one_identity_and_totals_deduplicate_reasons(self):
        self.issue(identity="same", content="one", reason="missing_endpoint")
        self.issue(identity="same", content="two", reason="missing_endpoint")
        self.issue(identity="same", content="three", reason="invalid_endpoint")
        _, detail, holds = self.ns["ingestion_checks"]({})
        text = "\n".join(detail)
        self.assertIn("rejected/missing_endpoint; 来源身份 1, 内容版本 2", text)
        self.assertIn("合计（跨原因组去重）: 来源身份 1, 内容版本 3", text)
        self.assertEqual(holds, set())

    def test_old_quality_rejections_do_not_block_completed_ok_recovery(self):
        names = ["a2a:agenstry", "a2a:crawl", "agenstry:crawl"]
        self.state = {"failing_sources": names[:], "stuck_sources": names[:]}
        self.issue(age=48 * HOUR, disposition="rejected")
        for source in names:
            self.run_row(source, status="ok")
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertIn("仅抓取状态", text)
        self.assertEqual(self.state["failing_sources"], [])
        self.assertEqual(self.state["stuck_sources"], [])
        self.mail.assert_not_called()

    def test_quality_only_notification_does_not_mark_watchdog_failed(self):
        self.issue()
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertIn("摄入数据质量提醒", self.mail.call_args.args[1])
        self.assertIn("不代表整轮失败", text)
        self.mail.return_value = False
        self.issue(reason="another_reason")
        self.assertEqual(self.main()[0], 1)

    def test_new_hold_alert_remains_failure_and_precedes_quality_in_subject(self):
        self.issue()
        self.issue(source="wellknown", disposition="hold", reason="wellknown_binding_target_changed")
        result, text = self.main()
        self.assertEqual(result, 1)
        self.assertIn("新增摄入待审原因组", text)
        self.assertIn("后台任务异常", self.mail.call_args.args[1])
        self.assertIn("wellknown_binding_target_changed", self.mail.call_args.args[1])

    def test_failed_quality_notification_retries_past_window_until_accepted(self):
        self.issue()
        self.mail.return_value = False
        self.assertEqual(self.main()[0], 1)
        self.assertTrue(self.state["pending_ingestion_reason_groups"])
        self.now += 25 * HOUR
        self.assertEqual(self.main()[0], 1)
        self.assertTrue(self.mail.called)
        self.mail.return_value = True
        self.assertEqual(self.main()[0], 0)
        self.assertTrue(self.mail.called)
        self.assertNotIn("pending_ingestion_reason_groups", self.state)
        self.assertEqual(self.main()[0], 0)
        self.mail.assert_not_called()

    def test_unaccepted_group_retries_even_after_active_issue_is_resolved(self):
        self.issue()
        self.mail.return_value = False
        self.main()
        self.conn.execute("UPDATE ingestion_issues SET resolved_at=?", (NOW,))
        self.conn.commit()
        self.mail.return_value = True
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertIn("首次提醒待送达；当前无活跃记录", text)
        self.assertNotIn("pending_ingestion_reason_groups", self.state)

    def warning(self, key='opaque-pair-key', **changes):
        row = {'warning_key': key, 'kind': 'a2a', 'listing_id': 9,
               'old_endpoint_hash': 'a' * 64, 'current_endpoint_hash': 'b' * 64,
               'change_type': 'path', 'detail_path': '/a2a/agents/fixture',
               'occurrences': 1, 'resolved_at': None, 'current_context': True}
        row.update(changes)
        self.warning_rows.append(row)
        return row

    def test_stale_warning_dedup_is_endpoint_pair_scoped(self):
        row = self.warning()
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertIn('来源端点失配警告', self.mail.call_args.args[1])
        self.assertIn('旧关联标记stale，当前有效主记录保留', text)
        self.assertIn('aaaaaaaaaaaa → bbbbbbbbbbbb', text)
        self.assertNotIn('PRIVATE', text)
        row['occurrences'] = 12
        self.assertEqual(self.main()[0], 0)
        self.mail.assert_not_called()
        self.warning('new-current-pair', current_endpoint_hash='c' * 64)
        _, text = self.main()
        self.assertIn('cccccccccccc', text)
        self.assertNotIn('bbbbbbbbbbbb', text)

    def test_stale_warning_does_not_block_completed_ok_recovery(self):
        self.state = {'failing_sources': ['a2a:crawl'], 'stuck_sources': ['a2a:crawl']}
        self.warning()
        self.run_row('a2a:crawl', status='ok')
        result, text = self.main()
        self.assertEqual(result, 0)
        self.assertIn('已恢复的 partial 源: a2a:crawl', text)
        self.assertEqual(self.state['failing_sources'], [])
        self.assertEqual(self.state['stuck_sources'], [])

    def test_stale_warning_pending_retries_past_window_until_accepted(self):
        row = self.warning()
        self.mail.return_value = False
        self.assertEqual(self.main()[0], 1)
        self.assertEqual(self.state['pending_source_endpoint_warnings'], ['opaque-pair-key'])
        self.now += 25 * HOUR
        row['resolved_at'] = self.now
        self.assertEqual(self.main()[0], 1)
        self.assertTrue(self.mail.called)
        self.mail.return_value = True
        self.assertEqual(self.main()[0], 0)
        self.assertNotIn('pending_source_endpoint_warnings', self.state)
        self.main()
        self.mail.assert_not_called()

    def test_deleted_or_privacy_hidden_warning_cancels_pending_delivery(self):
        self.warning()
        self.mail.return_value = False
        self.main()
        self.warning_rows.clear()
        self.assertEqual(self.main()[0], 0)
        self.mail.assert_not_called()
        self.assertNotIn('pending_source_endpoint_warnings', self.state)
        self.assertEqual(self.state['source_endpoint_warning_seen'], [])

    def test_stale_warning_with_true_hold_keeps_failure_severity(self):
        self.warning()
        self.issue(source='wellknown', disposition='hold', reason='wellknown_owner_endpoint_changed')
        result, text = self.main()
        self.assertEqual(result, 1)
        self.assertIn('后台任务异常', self.mail.call_args.args[1])
        self.assertIn('wellknown_owner_endpoint_changed', self.mail.call_args.args[1])
        self.assertIn('来源端点失配警告', text)

    def test_warning_renderer_never_calls_write_or_send_in_check_function(self):
        self.warning()
        before = list(self.conn.iterdump())
        warnings, detail = self.ns['source_warning_checks'](self.state)
        self.assertEqual(len(warnings), 1)
        self.assertIn('不代表发布者主动迁移', detail[0])
        self.assertEqual(list(self.conn.iterdump()), before)
        self.mail.assert_not_called()
        self.save.assert_not_called()

    def test_historical_warning_does_not_claim_old_hash_is_current_state(self):
        self.warning(current_context=False)
        _, text = self.main()
        self.assertIn('已记录历史端点变化，当前条目后续另有变化', text)
        self.assertNotIn('当前有效主记录保留', text)


if __name__ == "__main__":
    unittest.main()