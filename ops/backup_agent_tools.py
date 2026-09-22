#!/usr/bin/env python3
"""Root-run online backups; --root-dir redirects ALL source/local paths for tests.

Requires age, rsync, trusted SSH. Only public X25519 recipients are read here.
Retention means newest 3/14 completed archives, not calendar days. Local-only
never prunes. Snapshots are individually consistent, not a cross-DB transaction.
Coordinate config/key rotation externally; no software, TLS or SSH keys included.

At 4 completed local ciphertexts, new snapshots fail closed (also local-only).
Manual recovery: as root, stop scheduling, acquire
locked_directory(output, ".backup.lock"), and call
complete_offsite(existing_archive, seconds) on the newest pending ciphertext.
This retries full remote validation before either retention
or the durable success receipt. Never delete known-good archives to bypass the
cap. Failed uploads retain ciphertext; SIGKILL scratch is removed at the next
locked startup, not securely erased. Use encrypted scratch storage.
"""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid

DATABASES = {
    "main": ("opt/mcpserver/data/agent-tools.db", ("services", "mcp_servers", "a2a_agents", "users")),
    "hub": ("var/lib/agent-tools-hub/hub.db", ("sellers", "services", "usage_log", "needs")),
}
ENVS = ("opt/mcpserver/.env", "opt/agent-tools-hub/.env")
CONFIGS = (
    "etc/systemd/system/mcpserver.service", "etc/systemd/system/mcpserver.service.d/override.conf",
    "etc/systemd/system/mcpserver.service.d/ask.conf", "etc/agent-tools/ask.env",
    "etc/systemd/system/agent-tools-hub.service", "etc/nginx/nginx.conf", "etc/nginx/proxy_params",
    *(f"etc/nginx/sites-available/{site}{suffix}" for site in ("agent-tools.cloud", "hub.agent-tools.cloud") for suffix in ("", ".conf")),
    "etc/nginx/conf.d/agent-tools.cloud.conf", "etc/nginx/conf.d/hub.agent-tools.cloud.conf",
)
MEMBERS = {"data/main.db", "data/hub.db", *("config/" + p for p in ENVS + CONFIGS)}
NAME_PATTERN = r"agent-tools-[0-9]{8}T[0-9]{12}Z-[0-9a-f]{16}\.tar\.gz\.age"
STATE_PARTIAL_PATTERN = r"\.last-success-[0-9a-f]{32}\.json\.partial"
MAX_LOCAL_ARCHIVES = 4
RESERVE_BYTES = 2 * 1024 ** 3
REMOTE_RUNTIME = 1800
DEST = "root@43.130.32.180"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]


def private_dir(path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("unsafe directory")
    if not path.parent.exists():
        private_dir(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.geteuid():
        raise ValueError("unsafe owner")
    path.chmod(0o700)


def private_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)  # private even with inherited ACLs; usable with a restrictive umask
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def locked_directory(directory, name):
    private_dir(directory)
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "wb") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("unsafe lock")
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def cleanup_orphans(directory, prefix, partials=False):
    """Caller holds the directory's lock; never follow links or delete ciphertext."""
    for path in directory.iterdir():
        info = path.lstat()
        if info.st_uid != os.geteuid():
            continue
        if path.name.startswith(prefix) and stat.S_ISDIR(info.st_mode):
            # rmtree's fd-based implementation does not follow nested symlinks.
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError("safe scratch cleanup unavailable")
            shutil.rmtree(path)
        elif partials and stat.S_ISREG(info.st_mode) and (
                re.fullmatch(NAME_PATTERN + r"\.partial", path.name)
                or re.fullmatch(STATE_PARTIAL_PATTERN, path.name)):
            path.unlink()


def require_space(directory, needed):
    if shutil.disk_usage(directory).free < needed:
        raise OSError("insufficient backup scratch reserve")


def snapshot_bytes(source):
    """Logical pages include committed WAL growth absent from the main DB file."""
    regular(source)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        size = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
    return max(size, source.stat().st_size)


def regular(path):
    if path.resolve() != path.absolute() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("unsafe file")
    return path


def sha256(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def inspect_database(path, tables, deadline):
    if tuple(tables) not in [item[1] for item in DATABASES.values()]:
        raise ValueError("invalid table allowlist")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("integrity failure")
        return {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}


def snapshot(source, target, tables, seconds=600):
    deadline = time.monotonic() + seconds
    def progress(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError("snapshot deadline")
    regular(source)
    progress()
    with private_file(target):
        pass
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
        src.execute("PRAGMA query_only=ON")
        src.execute("BEGIN")
        src.execute("SELECT count(*) FROM sqlite_master").fetchone()  # pin one WAL snapshot
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=64, progress=progress, sleep=0.05)
            dst.execute("PRAGMA journal_mode=DELETE")
    progress()
    return inspect_database(target, tables, deadline)


def prune(directory, keep):
    if keep < 1:
        raise ValueError("invalid retention")
    files = sorted(p for p in directory.iterdir() if re.fullmatch(NAME_PATTERN, p.name)
                   and not p.is_symlink() and p.is_file())
    for path in files[:-keep]:
        path.unlink()


def command(args, seconds, **kwargs):
    subprocess.run(args, check=True, timeout=seconds, stderr=subprocess.DEVNULL,
                   **{"stdout": subprocess.DEVNULL, **kwargs})


# Executed only by the installed backup job, never at import time.
REMOTE = '''import hashlib, os, pathlib, re, stat
def main(name, expected, finalize):
    if not re.fullmatch(NAME_PATTERN, name) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError()
    directory = pathlib.Path("/var/backups/agent-tools")
    if directory.resolve() != directory.absolute() or directory.is_symlink():
        raise ValueError()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != 0 or not directory.is_dir():
        raise ValueError()
    directory.chmod(0o700)
    if not finalize:
        return
    partial = directory / (name + ".partial")
    try:
        fd = os.open(partial, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError()
            os.fchmod(stream.fileno(), 0o600)
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1048576), b""):
                digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError()
            os.fsync(stream.fileno())
        os.replace(partial, directory / name)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        files = sorted(p for p in directory.iterdir() if re.fullmatch(NAME_PATTERN, p.name)
                       and not p.is_symlink() and p.is_file())
        for path in files[:-14]:
            path.unlink()
    finally:
        partial.unlink(missing_ok=True)
'''


def push(archive, seconds):
    if not re.fullmatch(NAME_PATTERN, archive.name):
        raise ValueError("invalid name")
    digest = sha256(regular(archive))
    def remote(finalize):
        program = f"NAME_PATTERN = {NAME_PATTERN!r}\n" + REMOTE + f"\nmain({archive.name!r}, {digest!r}, {finalize!r})\n"
        command(SSH + [DEST, "/usr/bin/python3", "-"], seconds, input=program.encode())
    remote(False)
    command(["rsync", "--timeout=60", "--chmod=F600", "-e", " ".join(SSH), "--", str(archive),
             f"{DEST}:/var/backups/agent-tools/{archive.name}.partial"], seconds)
    # A job is successful only after the destination has decrypted the archive,
    # checked SQLite and verified that the saved Hub key decrypts its secrets.
    unit = "agent-tools-verify-" + hashlib.sha256(archive.name.encode()).hexdigest()[:32]
    command(SSH + [DEST, "systemd-run", "--quiet", "--wait", "--collect", "--unit=" + unit,
                   f"--property=RuntimeMaxSec={REMOTE_RUNTIME}", "--property=MemoryMax=384M",
                   "--property=UMask=0077", "--property=TimeoutStopSec=30", "--property=Type=exec",
                   "/usr/bin/python3", "/opt/agent-tools-backup/verify_agent_tools_backup.py",
                   f"/var/backups/agent-tools/{archive.name}.partial", "--timeout", str(min(seconds, REMOTE_RUNTIME))],
            REMOTE_RUNTIME + 120)
    remote(True)


def record_success(archive):
    """Root-owned 0600 receipt, published only after full offsite success."""
    output = archive.parent
    partial = output / (".last-success-" + uuid.uuid4().hex + ".json.partial")
    try:
        state = {"version": 1, "finished_at": time.time(), "archive": archive.name, "offsite": True}
        with private_file(partial) as stream:
            stream.write(json.dumps(state, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, output / "last-success.json")
        sync_dir(output)
    finally:
        partial.unlink(missing_ok=True)


def complete_offsite(archive, seconds):
    """Caller holds .backup.lock, including during manual pending recovery."""
    push(archive, seconds)  # full remote verify MUST precede promotion and all pruning
    prune(archive.parent, 3)
    sync_dir(archive.parent)
    record_success(archive)


def run_backup(root=Path("/"), local_only=False, seconds=600):
    root = root.resolve()
    if not 1 <= seconds <= 3600:
        raise ValueError("invalid deadline")
    if root == Path("/") and os.geteuid() != 0:
        raise PermissionError("root required")
    output = root / "var/backups/agent-tools"
    with locked_directory(output, ".backup.lock"):
        cleanup_orphans(output, ".plaintext-", partials=True)
        completed = sum(1 for p in output.iterdir() if re.fullmatch(NAME_PATTERN, p.name)
                        and stat.S_ISREG(p.lstat().st_mode))
        if completed >= MAX_LOCAL_ARCHIVES:
            raise RuntimeError("local ciphertext cap reached; recover existing archive under lock")
        require_space(output, 2 * sum(snapshot_bytes(root / source) for source, _ in DATABASES.values()) + RESERVE_BYTES)
        name = "agent-tools-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:16] + ".tar.gz.age"
        partial, final = output / (name + ".partial"), output / name
        try:
            with tempfile.TemporaryDirectory(prefix=".plaintext-", dir=output) as tmp:
                work = Path(tmp)
                private_dir(work)
                public = regular(root / "etc/agent-tools-backup/recipients.txt").read_text().splitlines()
                recipients = [line.strip() for line in public if line.strip() and not line.lstrip().startswith("#")]
                if not recipients or any(not re.fullmatch(r"age1[0-9a-z]{58}", r) for r in recipients):
                    raise ValueError("invalid public recipients")
                with private_file(work / "recipients.txt") as stream:
                    stream.write(("\n".join(recipients) + "\n").encode())
                manifest = {"version": 1, "files": {}}
                counts = {}
                for label, (source, tables) in DATABASES.items():
                    member = "data/" + label + ".db"
                    private_dir((work / member).parent)
                    counts[label] = snapshot(root / source, work / member, tables, seconds)
                    manifest["files"][member] = {"counts": counts[label]}
                for source in ENVS + CONFIGS:
                    path = root / source
                    if source in CONFIGS and not path.exists() and not path.is_symlink():
                        continue
                    regular(path)
                    member = "config/" + source
                    private_dir((work / member).parent)
                    with path.open("rb") as src, private_file(work / member) as dst:
                        shutil.copyfileobj(src, dst)
                    manifest["files"][member] = {}
                for member, info in manifest["files"].items():
                    info.update(sha256=sha256(work / member), size=(work / member).stat().st_size)
                with private_file(work / "manifest.json") as stream:
                    stream.write(json.dumps(manifest, sort_keys=True).encode())
                with private_file(work / "bundle.tar.gz") as stream, tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.USTAR_FORMAT) as tar:
                    for member in sorted({"manifest.json", *manifest["files"]}):
                        entry = tarfile.TarInfo(member)
                        entry.size, entry.mode = (work / member).stat().st_size, 0o600
                        with (work / member).open("rb") as src:
                            tar.addfile(entry, src)
                with private_file(partial) as encrypted:
                    command(["age", "--encrypt", "-R", str(work / "recipients.txt"), str(work / "bundle.tar.gz")], seconds, stdout=encrypted)
                    encrypted.flush()
                    os.fsync(encrypted.fileno())
                if not partial.stat().st_size:
                    raise ValueError("empty encryption output")
                os.replace(partial, final)
                sync_dir(output)
            if not local_only:
                complete_offsite(final, seconds)
            return {"status": "success", "archive": name, "offsite": not local_only, "counts": counts}
        finally:
            partial.unlink(missing_ok=True)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"status": "failure", "error": "invalid_arguments"}))
        raise SystemExit(2)


def interrupted(*_):
    raise InterruptedError()


def cli():
    parser = Parser(description=__doc__)
    parser.add_argument("--root-dir", type=Path, default=Path("/"))
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=600, help="per-stage deadline in seconds (1..3600)")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if not 1 <= args.timeout <= 3600:
            raise ValueError()
        print(json.dumps(run_backup(args.root_dir, args.local_only, args.timeout)))
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"status":"failure","error":"backup_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())