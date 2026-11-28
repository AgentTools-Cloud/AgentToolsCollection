"""On-chain measuring on a worker host: plan here, measure there, import here."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from directory import db, jobs, onchain_remote
from test_ingestion_pipeline import TemporaryDatabaseTestCase

PT_BASE = "0x" + "2" * 40
PT_POLY = "0x" + "1" * 40


class FakeWorker:
    """A worker directory on local disk, reached through patched ssh/scp."""

    def __init__(self, root):
        self.root = root
        self.started = 0

    def ssh(self, cmd, data=None, timeout=120):
        """The four commands onchain_remote sends, done on local disk."""
        cmd = cmd.replace(onchain_remote.WORKER_DIR, self.root)
        done = subprocess.CompletedProcess(cmd, 0, "", "")
        if "systemctl start" in cmd:
            self.started += 1
            return done
        if cmd.startswith("mkdir -p"):
            final = cmd.rsplit(" ", 1)[1]
            os.makedirs(os.path.join(self.root, "inbox"), exist_ok=True)
            os.makedirs(os.path.join(self.root, "outbox"), exist_ok=True)
            with open(final, "wb") as fh:
                fh.write(data)
            return done
        if cmd.startswith("cat "):
            path = cmd.split()[1]
            if os.path.exists(path):
                with open(path) as fh:
                    return subprocess.CompletedProcess(cmd, 0, fh.read(), "")
            return done
        if cmd.startswith("rm -f"):
            for path in cmd.split()[2:]:
                if os.path.exists(path):
                    os.unlink(path)
            return done
        raise AssertionError(f"unexpected worker command: {cmd}")

    def run(self, args, **kw):
        assert args[0] == "scp", args
        src = args[-2].split(":", 1)[1].replace(onchain_remote.WORKER_DIR, self.root)
        shutil.copyfile(src, args[-1])
        return subprocess.CompletedProcess(args, 0)

    def run_worker(self):
        inbox = os.path.join(self.root, "inbox")
        keys = [os.path.join(inbox, f) for f in os.listdir(inbox) if f.endswith(".keys.json")]
        assert len(keys) == 1, keys
        return onchain_remote.cmd_worker(keys[0], os.path.join(self.root, "outbox"))


class RemoteOnchainTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.ids = {}
        with db.writer() as conn:
            for slug, net, pt in [("on-polygon", "eip155:137", PT_POLY),
                                  ("on-base", "eip155:8453", PT_BASE),
                                  ("also-base", "eip155:8453", PT_BASE)]:
                _, sid = db.upsert_service(conn, dict(slug=slug, name=slug,
                                                      url="https://%s.example/x" % slug,
                                                      source="fixture", source_id=slug, health="ok"))
                conn.execute("UPDATE services SET payment=? WHERE id=?",
                             (json.dumps({"accepts": [{"network": net, "pay_to": pt}]}), sid))
                self.ids[slug] = sid
        tmp = tempfile.TemporaryDirectory(prefix="onchain-worker-")
        self.addCleanup(tmp.cleanup)
        self.fw = FakeWorker(tmp.name)
        self.enterContext(patch.dict(os.environ, {"AGENT_TOOLS_ONCHAIN_WORKER": "root@worker"}))
        self.enterContext(patch.object(onchain_remote, "_ssh", side_effect=self.fw.ssh))
        self.enterContext(patch.object(onchain_remote.subprocess, "run", side_effect=self.fw.run))
        self.enterContext(patch.object(onchain_remote, "_code_sha", return_value="abc123"))
        self.enterContext(patch.object(jobs, "cmd_rescore"))
        self.enterContext(patch.object(jobs.time, "sleep"))
        self.enterContext(patch.object(jobs.crawlers, "BULK_LOG_CHAINS", {"base": None}))
        self.enterContext(patch.object(jobs.crawlers, "fetch_chain_activity_bulk", side_effect=lambda a, chain="base", **k: (
            {x.lower(): {"tx": 7, "payers": 2, "payer_ids": {"p1", "p2"}, "raw_tx": 9} for x in a}, True)))
        self.enterContext(patch.object(jobs.crawlers, "fetch_payto_activity", return_value={
            "tx": 5, "payers": 1, "ok": True, "capped": False, "payer_ids": {"x"}, "raw_tx": 5}))

    def demand(self, slug):
        with closing(db.connect(read_only=True)) as conn:
            return tuple(conn.execute("SELECT payto_tx_30d, payto_payers_30d, tx_30d FROM services "
                                      "WHERE id=?", (self.ids[slug],)).fetchone())

    def test_round_trip(self):
        jobs.cmd_onchain()
        self.assertEqual(self.fw.started, 1)
        self.assertEqual(self.demand("on-base"), (None, None, None))   # nothing written yet
        onchain_remote.cmd_import()                                     # still measuring
        self.assertEqual(self.demand("on-base"), (None, None, None))
        self.fw.run_worker()
        onchain_remote.cmd_import()
        self.assertEqual(self.demand("on-base"), (7, 2, 7))
        self.assertEqual(self.demand("also-base"), (7, 2, 7))
        self.assertEqual(self.demand("on-polygon"), (5, 1, 5))
        with closing(db.connect(read_only=True)) as conn:
            rows = conn.execute("SELECT chain, address, tx_30d, raw_tx_30d FROM payto_activity "
                                "ORDER BY chain").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("base", PT_BASE, 7, 9), ("polygon", PT_POLY, 5, 5)])
        self.assertEqual(os.listdir(os.path.join(self.fw.root, "outbox")), [])
        self.assertEqual(onchain_remote._pending(), [])
        jobs.cmd_rescore.assert_called_once()

    def test_no_second_batch_while_one_is_out(self):
        jobs.cmd_onchain()
        jobs.cmd_onchain(refresh_all=True)
        self.assertEqual(self.fw.started, 1)

    def test_stale_batch_falls_back_to_local(self):
        jobs.cmd_onchain()
        (batch, path, meta), = onchain_remote._pending()
        meta["sent"] -= onchain_remote.PENDING_MAX_S + 1
        with open(os.path.join(path, "batch.json"), "w") as fh:
            json.dump(meta, fh)
        jobs.cmd_onchain(refresh_all=True)
        self.assertEqual(self.demand("on-base"), (7, 2, 7))     # measured here
        self.assertEqual(onchain_remote._pending(), [])

    def test_unreachable_worker_measures_here(self):
        with patch.object(onchain_remote, "_ssh", side_effect=subprocess.CalledProcessError(255, "ssh")):
            jobs.cmd_onchain()
        self.assertEqual(self.demand("on-polygon"), (5, 1, 5))

    def test_tampered_results_are_rejected(self):
        jobs.cmd_onchain()
        self.fw.run_worker()
        outbox = os.path.join(self.fw.root, "outbox")
        res = [f for f in os.listdir(outbox) if f.endswith(".gz")][0]
        with open(os.path.join(outbox, res), "ab") as fh:
            fh.write(b"x")
        onchain_remote.cmd_import()
        self.assertEqual(self.demand("on-base"), (None, None, None))
        self.assertEqual(onchain_remote._pending(), [])

    def test_listing_changed_since_plan_is_not_written(self):
        jobs.cmd_onchain()
        with db.writer() as conn:
            conn.execute("UPDATE services SET url='https://moved.example/' WHERE id=?",
                         (self.ids["on-base"],))
        self.fw.run_worker()
        onchain_remote.cmd_import()
        self.assertEqual(self.demand("on-base"), (None, None, None))
        self.assertEqual(self.demand("also-base"), (7, 2, 7))

    def test_local_flag_ignores_worker(self):
        jobs.cmd_onchain(local=True)
        self.assertEqual(self.fw.started, 0)
        self.assertEqual(self.demand("on-base"), (7, 2, 7))


if __name__ == "__main__":
    unittest.main()
