"""Local stdlib tests: synthetic data only, all SSH/rsync/age calls mocked."""
from contextlib import closing, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

OPS = Path(__file__).resolve().parents[1] / "ops"


def load(name):
    spec = importlib.util.spec_from_file_location(name, OPS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


backup = load("backup_agent_tools")
verify = load("verify_agent_tools_backup")
KEY = "A" * 43 + "="  # synthetic, never a production credential
TOKEN = "synthetic-valid-ciphertext"


class FakeFernet:
    calls = []

    def __init__(self, key):
        if key != KEY.encode():
            raise ValueError("synthetic wrong key")

    def decrypt(self, token):
        if token != TOKEN.encode():
            raise ValueError("synthetic invalid token")
        self.calls.append(token)
        return b"synthetic plaintext"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "var/backups/agent-tools"
        for label, (source, tables) in backup.DATABASES.items():
            path = self.root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as db:
                for table in tables:
                    extra = ", upstream_auth_enc TEXT" if label == "hub" and table == "services" else ""
                    db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY{extra})")
                    db.execute(f"INSERT INTO {table}(id) VALUES (1)")
                if label == "hub":
                    db.execute("UPDATE services SET upstream_auth_enc=?", (TOKEN,))
                    db.executemany("INSERT INTO services(upstream_auth_enc) VALUES (?)", [("",), (None,), (TOKEN,)])
                db.commit()
        for source in backup.ENVS + backup.CONFIGS[:2]:
            path = self.root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic config\n")
        (self.root / backup.ENVS[1]).write_text(f'export FERNET_KEY="{KEY}" # synthetic\n')
        self.recipient = self.root / "etc/agent-tools-backup/recipients.txt"
        self.recipient.parent.mkdir(parents=True)
        self.recipient.write_text("# Public only\n" + "age1" + "q" * 58 + "\n")
        self.identity = self.root / "identity.agekey"
        self.identity.write_text("synthetic identity")
        self.identity.chmod(0o600)
        self.stage = self.root / "verify"
        self.calls = []
        self.bundle = None
        self.runner = patch.object(backup.subprocess, "run", side_effect=self.fake_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.space = patch.object(backup.shutil, "disk_usage", return_value=types.SimpleNamespace(free=100 * 1024 ** 3))
        self.space.start()
        self.addCleanup(self.space.stop)
        fake_crypto = types.ModuleType("cryptography")
        fake_fernet = types.ModuleType("cryptography.fernet")
        fake_fernet.Fernet = FakeFernet
        modules = patch.dict(sys.modules, {"cryptography": fake_crypto, "cryptography.fernet": fake_fernet})
        modules.start()
        self.crypto_modules = modules
        self.addCleanup(modules.stop)
        FakeFernet.calls = []

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        self.assertTrue(kwargs["check"])
        self.assertGreater(kwargs["timeout"], 0)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        if args[0] == "age":
            if "--encrypt" in args:
                bundle = Path(args[-1])
                self.bundle = bundle.read_bytes()
                for directory, _, files in os.walk(bundle.parent):
                    self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
                    for name in files:
                        self.assertEqual(stat.S_IMODE((Path(directory) / name).stat().st_mode), 0o600)
                kwargs["stdout"].write(b"synthetic-age-ciphertext")
            else:
                kwargs["stdout"].write(self.bundle)
        return subprocess.CompletedProcess(args, 0)

    def make_backup(self, local_only=True):
        result = backup.run_backup(self.root, local_only, 10)
        return self.output / result["archive"]

    def assert_clean(self):
        self.assertFalse(list(self.output.glob(".plaintext-*")))
        self.assertFalse(list(self.output.glob("*.partial")))
        if self.stage.exists():
            self.assertEqual([p.name for p in self.stage.iterdir()], [".verify.lock"])

    def old_files(self, directory, count):
        directory.mkdir(parents=True, exist_ok=True)
        paths = [directory / f"agent-tools-20200101T000000000000Z-{n:016x}.tar.gz.age" for n in range(count)]
        for path in paths:
            path.write_bytes(b"encrypted fixture")
        return paths

    def rewrite_bundle(self, mutate):
        with tarfile.open(fileobj=io.BytesIO(self.bundle), mode="r:gz") as tar:
            data = {entry.name: tar.extractfile(entry).read() for entry in tar}
        mutate(data)
        target = io.BytesIO()
        with tarfile.open(fileobj=target, mode="w:gz") as tar:
            for name, value in data.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(value), 0o600
                tar.addfile(info, io.BytesIO(value))
        self.bundle = target.getvalue()

    def test_archive_manifest_allowlist_and_modes(self):
        forbidden = self.root / "root/.ssh/id_ed25519"
        forbidden.parent.mkdir(parents=True)
        forbidden.write_text("synthetic forbidden secret")
        archive = self.make_backup()
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        with tarfile.open(fileobj=io.BytesIO(self.bundle), mode="r:gz") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            self.assertEqual(set(tar.getnames()), set(manifest["files"]) | {"manifest.json"})
            for member, info in manifest["files"].items():
                self.assertIn(member, backup.MEMBERS)
                self.assertFalse(member.startswith("/"))
                self.assertNotIn("..", Path(member).parts)
                content = tar.extractfile(member).read()
                self.assertEqual(info["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(info["size"], len(content))
                self.assertEqual(tar.getmember(member).mode, 0o600)
            self.assertEqual(manifest["files"]["data/main.db"]["counts"]["users"], 1)
        self.assertNotIn(b"synthetic forbidden secret", self.bundle)
        self.assertEqual([call[0][0] for call in self.calls], ["age"])
        self.assert_clean()

    def test_wal_committed_snapshot_is_independently_consistent(self):
        source = self.root / backup.DATABASES["main"][0]
        tables = backup.DATABASES["main"][1]
        with closing(sqlite3.connect(source)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("INSERT INTO users VALUES (2)")
            writer.commit()
            writer.execute("INSERT INTO users VALUES (3)")
            self.assertTrue(Path(str(source) + "-wal").exists())
            target = self.root / "snapshot.db"
            counts = backup.snapshot(source, target, tables, 10)
            self.assertEqual(counts["users"], 2)
            writer.commit()
            self.assertEqual(backup.inspect_database(target, tables, backup.time.monotonic() + 5), counts)
            self.assertEqual(writer.execute("SELECT count(*) FROM users").fetchone()[0], 3)

    def test_source_readonly_small_pages_and_deadline(self):
        source = self.root / backup.DATABASES["main"][0]
        real_connect = sqlite3.connect
        traces, opened, pages = [], [], []
        class SourceConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                pages.append(kwargs["pages"])
                return super().backup(target, **kwargs)
        def connect(database, **kwargs):
            opened.append((database, kwargs.copy()))
            if str(database) == source.as_uri() + "?mode=ro":
                kwargs["factory"] = SourceConnection
                db = real_connect(database, **kwargs)
                db.set_trace_callback(traces.append)
                return db
            return real_connect(database, **kwargs)
        with patch.object(backup.sqlite3, "connect", side_effect=connect):
            backup.snapshot(source, self.root / "snapshot.db", backup.DATABASES["main"][1], 10)
        self.assertEqual(pages, [64])
        self.assertTrue(opened[0][1]["uri"])
        self.assertIn("PRAGMA query_only=ON", traces)
        self.assertFalse(any("integrity_check" in sql for sql in traces))
        with patch.object(backup.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(TimeoutError):
                backup.snapshot(source, self.root / "timed-out.db", backup.DATABASES["main"][1], 1)

    def test_retention_only_exact_completed_regular_files(self):
        paths = self.old_files(self.output, 5)
        extra = [self.output / "other.age", self.output / (paths[0].name + ".partial")]
        for path in extra:
            path.write_text("preserve")
        symlink = self.output / "agent-tools-20190101T000000000000Z-ffffffffffffffff.tar.gz.age"
        symlink.symlink_to(extra[0])
        directory = self.output / "agent-tools-20180101T000000000000Z-ffffffffffffffff.tar.gz.age"
        directory.mkdir()
        backup.prune(self.output, 3)
        self.assertEqual([path.exists() for path in paths], [False, False, True, True, True])
        backup.prune(self.output, 3)
        self.assertTrue(all(path.exists() for path in extra))
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(directory.is_dir())
        with self.assertRaises(ValueError):
            backup.prune(self.output, 0)

    def test_upload_order_and_retention_gate(self):
        old = self.old_files(self.output, 3)
        archive = self.make_backup(local_only=False)
        self.assertEqual([call[0][0] for call in self.calls], ["age", "ssh", "rsync", "ssh", "ssh"])
        for args, kwargs in self.calls[1:]:
            self.assertIn("StrictHostKeyChecking=yes", " ".join(args))
            self.assertIn("BatchMode=yes", " ".join(args))
            self.assertIn("ConnectTimeout=15", " ".join(args))
            self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(self.calls[2][0][-1].endswith(archive.name + ".partial"))
        self.assertIn("/opt/agent-tools-backup/verify_agent_tools_backup.py", self.calls[3][0])
        args, kwargs = self.calls[3]
        for arg in ("systemd-run", "--quiet", "--wait", "--collect", "--property=RuntimeMaxSec=1800",
                    "--property=MemoryMax=384M", "--property=UMask=0077", "--property=Type=exec"):
            self.assertIn(arg, args)
        self.assertIn("--unit=agent-tools-verify-" + hashlib.sha256(archive.name.encode()).hexdigest()[:32], args)
        self.assertGreater(kwargs["timeout"], backup.REMOTE_RUNTIME + 30)
        self.assertIn(backup.sha256(archive).encode(), self.calls[4][1]["input"])
        self.assertEqual([path.exists() for path in old], [False, True, True])
        state_path = self.output / "last-success.json"
        state = json.loads(state_path.read_text())
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["archive"], archive.name)
        self.assertIs(state["offsite"], True)
        self.assertLess(abs(backup.time.time() - state["finished_at"]), 10)
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertEqual(state_path.stat().st_uid, os.geteuid())
        self.assert_clean()

    def test_local_only_does_not_prune(self):
        old = self.old_files(self.output, 3)
        self.make_backup()
        self.assertTrue(all(path.exists() for path in old))
        self.assertFalse((self.output / "last-success.json").exists())

    def test_upload_failure_never_prunes_and_keeps_encrypted_copy(self):
        old = self.old_files(self.output, 3)
        receipt = self.output / "last-success.json"
        receipt.write_text('{"previous": "success"}')
        for fail_command in ("ssh", "rsync", "verify", "finalize"):
            with self.subTest(fail_command=fail_command):
                ssh_calls = 0
                def fail(args, **kwargs):
                    nonlocal ssh_calls
                    if args[0] == "ssh":
                        ssh_calls += 1
                    if args[0] == fail_command or (fail_command == "verify" and ssh_calls == 2) or (fail_command == "finalize" and ssh_calls == 3):
                        raise subprocess.TimeoutExpired(args, 10)
                    return self.fake_run(args, **kwargs)
                with patch.object(backup.subprocess, "run", side_effect=fail), patch.object(backup, "prune") as prune:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        self.make_backup(local_only=False)
                    prune.assert_not_called()
                self.assertTrue(all(path.exists() for path in old))
                self.assertEqual(receipt.read_text(), '{"previous": "success"}')
                pending = set(self.output.glob("*.age")) - set(old)
                self.assertEqual(len(pending), 1)
                # Remove only this synthetic test artifact to isolate failure stages.
                pending.pop().unlink()
                self.assert_clean()

    def test_repeated_upload_failures_stop_at_four_without_new_snapshot(self):
        old = self.old_files(self.output, 2)
        with patch.object(backup, "push", side_effect=OSError("upload unavailable")), patch.object(backup, "prune") as prune:
            for _ in range(2):
                with self.assertRaises(OSError):
                    self.make_backup(local_only=False)
            completed = set(self.output.glob("*.age"))
            self.assertEqual(len(completed), 4)
            for local_only in (False, True):
                with patch.object(backup, "snapshot") as snapshot, self.assertRaises(RuntimeError):
                    self.make_backup(local_only=local_only)
                snapshot.assert_not_called()
                self.assertEqual(set(self.output.glob("*.age")), completed)
            prune.assert_not_called()
        self.assertTrue(all(p.exists() for p in old))
        self.assertFalse((self.output / "last-success.json").exists())
        self.assert_clean()

    def test_manual_pending_recovery_verifies_before_pruning_and_receipt(self):
        completed = self.old_files(self.output, 4)
        with backup.locked_directory(self.output, ".backup.lock"):
            backup.complete_offsite(completed[-1], 10)
        self.assertEqual([call[0][0] for call in self.calls], ["ssh", "rsync", "ssh", "ssh"])
        self.assertIn("systemd-run", self.calls[2][0])
        self.assertFalse(completed[0].exists())
        self.assertEqual(json.loads((self.output / "last-success.json").read_text())["archive"], completed[-1].name)

    def test_source_space_reserve_before_snapshot_and_wal_size(self):
        source = self.root / backup.DATABASES["main"][0]
        with closing(sqlite3.connect(source)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE growth (payload BLOB)")
            db.execute("INSERT INTO growth VALUES (zeroblob(1048576))")
            db.commit()
            logical = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
            self.assertGreater(logical, source.stat().st_size)
            self.assertEqual(backup.snapshot_bytes(source), logical)
            needed = 2 * sum(backup.snapshot_bytes(self.root / s) for s, _ in backup.DATABASES.values()) + backup.RESERVE_BYTES
            with patch.object(backup.shutil, "disk_usage", return_value=types.SimpleNamespace(free=needed - 1)), patch.object(backup, "snapshot") as snapshot:
                with self.assertRaises(OSError):
                    self.make_backup()
                snapshot.assert_not_called()
            with patch.object(backup.shutil, "disk_usage", return_value=types.SimpleNamespace(free=needed)):
                self.make_backup()
        self.assert_clean()

    def test_locked_source_cleanup_exact_orphans_and_no_link_following(self):
        old = self.old_files(self.output, 1)
        outside = self.root / "outside"
        outside.mkdir()
        secret = outside / "preserve"
        secret.write_text("synthetic")
        orphan = self.output / ".plaintext-orphan"
        orphan.mkdir()
        (orphan / "link").symlink_to(outside, target_is_directory=True)
        (orphan / "secret").write_text("synthetic plaintext")
        link = self.output / ".plaintext-link"
        link.symlink_to(outside, target_is_directory=True)
        partial = self.output / (old[0].name + ".partial")
        partial.write_text("incomplete")
        state_partial = self.output / (".last-success-" + "a" * 32 + ".json.partial")
        state_partial.write_text("incomplete")
        other = self.output / "other.partial"
        other.write_text("preserve")
        partial_link = self.output / (old[0].name.replace("20200101", "20200102") + ".partial")
        partial_link.symlink_to(secret)
        foreign = self.output / ".plaintext-foreign"
        foreign.mkdir()
        real_lstat = Path.lstat
        def lstat(path):
            info = real_lstat(path)
            return types.SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=info.st_mode) if path == foreign else info
        with patch.object(Path, "lstat", lstat):
            self.make_backup()
        self.assertFalse(orphan.exists())
        self.assertFalse(partial.exists())
        self.assertFalse(state_partial.exists())
        for p in (secret, link, partial_link, other, foreign, old[0]):
            self.assertTrue(p.exists())

    def test_overlapping_backup_does_not_clean_active_scratch(self):
        backup.private_dir(self.output)
        scratch = self.output / ".plaintext-active"
        scratch.mkdir()
        with backup.locked_directory(self.output, ".backup.lock"):
            with self.assertRaises(BlockingIOError):
                self.make_backup()
            self.assertTrue(scratch.exists())

    def test_plaintext_permissions_with_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            archive = self.make_backup()
            self.assertEqual(verify.verify(archive, self.identity, self.stage, 10)["status"], "success")
        finally:
            os.umask(previous)
        self.assert_clean()

    def test_atomic_receipt_replace_failure_preserves_previous_success(self):
        archive = self.make_backup()
        receipt = self.output / "last-success.json"
        receipt.write_text("previous success")
        with patch.object(backup.os, "replace", side_effect=OSError("interrupted publication")):
            with self.assertRaises(OSError):
                backup.record_success(archive)
        self.assertEqual(receipt.read_text(), "previous success")
        self.assertFalse(list(self.output.glob(".last-success-*.partial")))

    def test_receipt_fsync_atomic_replace_then_directory_fsync(self):
        archive = self.make_backup()
        events = []
        real_fsync, real_replace = os.fsync, os.replace
        def fsync(fd):
            events.append("directory-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
            return real_fsync(fd)
        def replace(source, target):
            self.assertEqual(Path(target), self.output / "last-success.json")
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            self.assertEqual(json.loads(Path(source).read_text())["archive"], archive.name)
            events.append("replace")
            return real_replace(source, target)
        with patch.object(backup.os, "fsync", side_effect=fsync), patch.object(backup.os, "replace", side_effect=replace):
            backup.record_success(archive)
        self.assertEqual(events, ["file-fsync", "replace", "directory-fsync"])
        receipt = (self.output / "last-success.json").read_bytes()
        self.make_backup(local_only=True)
        self.assertEqual((self.output / "last-success.json").read_bytes(), receipt)

    def test_success_receipt_follows_remote_promotion_and_local_retention(self):
        events = []
        real_run, real_prune, real_record = self.fake_run, backup.prune, backup.record_success
        def run(args, **kwargs):
            events.append("verify" if "systemd-run" in args else "finalize" if b", True)" in kwargs.get("input", b"") else args[0])
            return real_run(args, **kwargs)
        def prune(*args):
            events.append("prune")
            return real_prune(*args)
        def record(*args):
            events.append("receipt")
            return real_record(*args)
        with patch.object(backup.subprocess, "run", side_effect=run), patch.object(backup, "prune", side_effect=prune), patch.object(backup, "record_success", side_effect=record):
            self.make_backup(local_only=False)
        self.assertEqual(events, ["age", "ssh", "rsync", "verify", "finalize", "prune", "receipt"])

    def test_prune_failure_does_not_advance_receipt(self):
        with patch.object(backup, "prune", side_effect=OSError("retention failed")):
            with self.assertRaises(OSError):
                self.make_backup(local_only=False)
        self.assertFalse((self.output / "last-success.json").exists())

    def test_encryption_failure_cleans_plaintext_and_partial(self):
        def fail(args, **kwargs):
            kwargs["stdout"].write(b"incomplete")
            raise subprocess.CalledProcessError(1, args, stderr="synthetic secret")
        with patch.object(backup.subprocess, "run", side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                self.make_backup()
        self.assertEqual(list(self.output.glob("*.age")), [])
        self.assert_clean()

    def test_missing_env_and_private_recipient_fail_closed(self):
        env = self.root / backup.ENVS[0]
        env.unlink()
        with self.assertRaises(FileNotFoundError):
            self.make_backup()
        self.assert_clean()
        env.write_text("synthetic")
        self.recipient.write_text("AGE-SECRET-KEY-DO-NOT-READ\n")
        with self.assertRaises(ValueError):
            self.make_backup()
        self.assertEqual(self.calls, [])
        self.assert_clean()

    def test_config_symlink_rejected(self):
        env = self.root / backup.ENVS[0]
        env.unlink()
        env.symlink_to(self.identity)
        with self.assertRaises(ValueError):
            self.make_backup()
        self.assert_clean()

    def test_overlapping_backup_is_rejected(self):
        backup.private_dir(self.output)
        with (self.output / ".backup.lock").open("wb") as lock:
            backup.fcntl.flock(lock, backup.fcntl.LOCK_EX | backup.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.make_backup()
        self.assertEqual(self.calls, [])

    def test_push_rejects_shell_injection_before_subprocess(self):
        with self.assertRaises(ValueError):
            backup.push(self.root / "bad;touch-PWNED.age", 10)
        self.assertEqual(self.calls, [])

    def test_remote_program_checksum_atomic_promotion_and_14_retention(self):
        destination = self.root / "destination"
        old = self.old_files(destination, 16)
        name = "agent-tools-20260922T000000000000Z-eeeeeeeeeeeeeeee.tar.gz.age"
        partial = destination / (name + ".partial")
        partial.write_bytes(b"synthetic ciphertext")
        # Execute only against this temporary directory; no SSH and no real root path.
        program = backup.REMOTE.replace('pathlib.Path("/var/backups/agent-tools")', "pathlib.Path(TEST_DEST)")
        program = program.replace("directory.stat().st_uid != 0", "directory.stat().st_uid != os.geteuid()")
        namespace = {"NAME_PATTERN": backup.NAME_PATTERN, "TEST_DEST": str(destination)}
        exec(compile(program, "<isolated-remote-check>", "exec"), namespace)
        with self.assertRaises(ValueError):
            namespace["main"](name, "0" * 64, True)
        self.assertTrue(all(path.exists() for path in old))
        self.assertFalse(partial.exists())
        partial.write_bytes(b"synthetic ciphertext")
        namespace["main"](name, backup.sha256(partial), True)
        self.assertEqual(len(list(destination.glob("*.age"))), 14)
        self.assertFalse(any(path.exists() for path in old[:3]))
        self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o600)

    def test_restore_verifies_counts_and_every_nonempty_ciphertext(self):
        archive = self.make_backup()
        result = verify.verify(archive, self.identity, self.stage, 10)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"]["hub"]["services"], 4)
        self.assertEqual(result["decrypted_upstream_auth"], 2)
        self.assertEqual(FakeFernet.calls, [TOKEN.encode(), TOKEN.encode()])
        self.assert_clean()

    def test_destination_lock_orphan_cleanup_and_reserve(self):
        archive = self.make_backup()
        backup.private_dir(self.stage)
        orphan = self.stage / ".verify-orphan"
        orphan.mkdir()
        (orphan / "plaintext").write_text("synthetic")
        unrelated = self.stage / ".plaintext-unrelated"
        unrelated.mkdir()
        with backup.locked_directory(self.stage, ".verify.lock"):
            with self.assertRaises(BlockingIOError):
                verify.verify(archive, self.identity, self.stage, 10)
            self.assertTrue(orphan.exists())
        self.calls.clear()
        needed = verify.MAX_BYTES + archive.stat().st_size + backup.RESERVE_BYTES
        with patch.object(backup.shutil, "disk_usage", return_value=types.SimpleNamespace(free=needed - 1)):
            with self.assertRaises(OSError):
                verify.verify(archive, self.identity, self.stage, 10)
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.calls, [])
        unrelated.rmdir()
        with patch.object(backup.shutil, "disk_usage", return_value=types.SimpleNamespace(free=needed)):
            self.assertEqual(verify.verify(archive, self.identity, self.stage, 10)["status"], "success")
        self.assert_clean()

    def test_destination_ciphertext_and_total_extraction_caps(self):
        archive = self.make_backup()
        self.calls.clear()
        self.assertEqual(verify.MAX_BYTES, 8 * 1024 ** 3)
        with patch.object(verify, "MAX_BYTES", archive.stat().st_size - 1):
            with self.assertRaises(ValueError):
                verify.verify(archive, self.identity, self.stage, 10)
        self.assertEqual(self.calls, [])
        tarpath = self.root / "aggregate.tar.gz"
        with tarfile.open(tarpath, "w:gz") as tar:
            for name in ("data/main.db", "data/hub.db"):
                member = tarfile.TarInfo(name)
                member.size = 2
                tar.addfile(member, io.BytesIO(b"xx"))
        with patch.object(verify, "MAX_BYTES", 3), self.assertRaises(ValueError):
            verify.extract(tarpath, self.root / "aggregate")
        self.assert_clean()

    def test_sigterm_handler_cleans_source_and_destination_plaintext(self):
        archive = self.make_backup()
        def interrupted(args, **kwargs):
            kwargs["stdout"].write(b"interrupted plaintext")
            backup.interrupted()
        with patch.object(backup.subprocess, "run", side_effect=interrupted):
            with self.assertRaises(InterruptedError):
                self.make_backup()
            self.assert_clean()
            with self.assertRaises(InterruptedError):
                verify.verify(archive, self.identity, self.stage, 10)
            self.assert_clean()

    def test_restore_rejects_hash_count_and_membership_mismatches(self):
        archive = self.make_backup()
        original = self.bundle
        def hash_mismatch(data):
            data["config/opt/mcpserver/.env"] += b"changed"
        def count_mismatch(data):
            manifest = json.loads(data["manifest.json"])
            manifest["files"]["data/main.db"]["counts"]["users"] += 1
            data["manifest.json"] = json.dumps(manifest).encode()
        def missing_member(data):
            del data["data/hub.db"]
        for change in (hash_mismatch, count_mismatch, missing_member):
            with self.subTest(change=change.__name__):
                self.bundle = original
                self.rewrite_bundle(change)
                with self.assertRaises(ValueError):
                    verify.verify(archive, self.identity, self.stage, 10)
                self.assert_clean()

    def test_restore_bad_fernet_key_and_corrupt_token(self):
        archive = self.make_backup()
        def missing_key(data):
            member = "config/opt/agent-tools-hub/.env"
            data[member] = b"OTHER_KEY=synthetic\n"
            manifest = json.loads(data["manifest.json"])
            manifest["files"][member].update(size=len(data[member]), sha256=hashlib.sha256(data[member]).hexdigest())
            data["manifest.json"] = json.dumps(manifest).encode()
        self.rewrite_bundle(missing_key)
        with self.assertRaises(ValueError):
            verify.verify(archive, self.identity, self.stage, 10)
        self.assert_clean()
        with closing(sqlite3.connect(self.root / backup.DATABASES["hub"][0])) as db:
            db.execute("UPDATE services SET upstream_auth_enc='broken' WHERE id=4")
            db.commit()
        archive = self.make_backup()
        with self.assertRaises(ValueError):
            verify.verify(archive, self.identity, self.stage, 10)
        self.assert_clean()

    def test_restore_rejects_corrupt_database_even_with_matching_hash(self):
        archive = self.make_backup()
        def corrupt(data):
            member = "data/main.db"
            data[member] = b"not a SQLite database"
            manifest = json.loads(data["manifest.json"])
            manifest["files"][member].update(size=len(data[member]), sha256=hashlib.sha256(data[member]).hexdigest())
            data["manifest.json"] = json.dumps(manifest).encode()
        self.rewrite_bundle(corrupt)
        with self.assertRaises(sqlite3.DatabaseError):
            verify.verify(archive, self.identity, self.stage, 10)
        self.assert_clean()

    def test_real_fernet_if_already_installed(self):
        self.crypto_modules.stop()
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError:
            self.skipTest("optional cryptography not installed; no packages installed by tests")
        key = Fernet.generate_key()
        token = Fernet(key).encrypt(b"synthetic fixture value").decode()
        (self.root / backup.ENVS[1]).write_text("FERNET_KEY=" + key.decode() + "\n")
        with closing(sqlite3.connect(self.root / backup.DATABASES["hub"][0])) as db:
            db.execute("UPDATE services SET upstream_auth_enc=? WHERE upstream_auth_enc=?", (token, TOKEN))
            db.commit()
        archive = self.make_backup()
        self.assertEqual(verify.verify(archive, self.identity, self.stage, 10)["decrypted_upstream_auth"], 2)
        with closing(sqlite3.connect(self.root / backup.DATABASES["hub"][0])) as db:
            db.execute("UPDATE services SET upstream_auth_enc='bad' WHERE id=4")
            db.commit()
        archive = self.make_backup()
        with self.assertRaises(InvalidToken):
            verify.verify(archive, self.identity, self.stage, 10)
        self.assert_clean()

    def test_restore_rejects_ambiguous_key_assignment(self):
        (self.root / backup.ENVS[1]).write_text(f"FERNET_KEY={KEY}\nFERNET_KEY=\n")
        archive = self.make_backup()
        with self.assertRaises(ValueError):
            verify.verify(archive, self.identity, self.stage, 10)
        self.assert_clean()

    def test_extraction_rejects_traversal_links_duplicates_and_oversize(self):
        cases = [("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
                 ("config/../escape", tarfile.REGTYPE), ("data/main.db", tarfile.SYMTYPE),
                 ("data/main.db", tarfile.LNKTYPE), ("data/main.db", tarfile.FIFOTYPE),
                 ("data/main.db", tarfile.DIRTYPE), ("unexpected", tarfile.REGTYPE)]
        for i, (name, kind) in enumerate(cases):
            with self.subTest(name=name, kind=kind):
                archive = self.root / f"malicious-{i}.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo(name)
                    member.type, member.linkname = kind, "../escape"
                    tar.addfile(member)
                with self.assertRaises(ValueError):
                    verify.extract(archive, self.root / f"extract-{i}")
        archive = self.root / "duplicate.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.addfile(tarfile.TarInfo("manifest.json"))
            tar.addfile(tarfile.TarInfo("manifest.json"))
        with self.assertRaises(ValueError):
            verify.extract(archive, self.root / "duplicate")
        archive = self.root / "oversize.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("data/main.db")
            member.size = 2
            tar.addfile(member, io.BytesIO(b"xx"))
        with patch.object(verify, "MAX_BYTES", 1), self.assertRaises(ValueError):
            verify.extract(archive, self.root / "oversize")
        self.assertFalse((self.root / "escape").exists())

    def test_decryption_failure_and_unsafe_identity_cleanup(self):
        archive = self.make_backup()
        def fail(args, **kwargs):
            kwargs["stdout"].write(b"partial plaintext")
            raise subprocess.CalledProcessError(1, args)
        with patch.object(backup.subprocess, "run", side_effect=fail), self.assertRaises(subprocess.CalledProcessError):
            verify.verify(archive, self.identity, self.stage, 10)
        self.assert_clean()
        self.identity.chmod(0o644)
        with self.assertRaises(ValueError):
            verify.verify(archive, self.identity, self.stage, 10)

    def test_cli_failures_do_not_disclose_exception_or_arguments(self):
        for module, target, args in ((backup, "run_backup", ["backup"]),
                                     (verify, "verify", ["verify", "archive"])):
            output = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(module, target, side_effect=ValueError("SYNTHETIC_SECRET")), redirect_stdout(output):
                self.assertEqual(module.cli(), 1)
            self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "failure")
            output = io.StringIO()
            with patch.object(sys, "argv", ["script", "--SYNTHETIC_SECRET"]), redirect_stdout(output), self.assertRaises(SystemExit):
                module.cli()
            self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())

    def test_help_does_not_run_backup_or_verify(self):
        for module in (backup, verify):
            output = io.StringIO()
            with patch.object(sys, "argv", ["script", "--help"]), redirect_stdout(output), self.assertRaises(SystemExit) as error:
                module.cli()
            self.assertEqual(error.exception.code, 0)
            self.assertIn("usage:", output.getvalue())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()