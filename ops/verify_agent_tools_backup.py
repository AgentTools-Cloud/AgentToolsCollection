#!/usr/bin/env python3
"""Isolated destination-only age restore check; never starts or restores services.

Keep this file beside backup_agent_tools.py; selected Python needs cryptography.
Identity stays on the destination. Temporary plaintext is unlinked, not securely
erased; use encrypted scratch storage. age encryption does not authenticate sender.
The source invokes this under systemd with a 30-minute runtime and 384MiB memory
limit. The scratch lock serializes verifiers; the next locked invocation removes
SIGKILL leftovers. Direct manual runs must use the same systemd resource limits.
"""
import json
import os
from pathlib import Path
import re
import signal
import tarfile
import tempfile
import time
from contextlib import closing
import sqlite3

import backup_agent_tools as backup

MAX_BYTES = 8 * 1024 ** 3


def extract(archive, work):
    seen, total = set(), 0
    with tarfile.open(archive, "r|gz") as tar:
        for member in tar:
            if (member.name not in backup.MEMBERS | {"manifest.json"} or member.name in seen
                    or not member.isfile() or member.issparse() or member.size < 0):
                raise ValueError("invalid archive member")
            total += member.size
            if total > MAX_BYTES or (member.name == "manifest.json" and member.size > 1024 ** 2):
                raise ValueError("oversized archive")
            seen.add(member.name)
            path = work / member.name
            backup.private_dir(path.parent)
            with closing(tar.extractfile(member)) as src, backup.private_file(path) as dst:
                backup.shutil.copyfileobj(src, dst)
    return seen - {"manifest.json"}


def verify_hub(work):
    from cryptography.fernet import Fernet  # required only on the verification host
    text = (work / "config/opt/agent-tools-hub/.env").read_text()
    keys = re.findall(r'''(?im)^[ \t]*(?:export[ \t]+)?FERNET_KEY[ \t]*=[ \t]*(['"]?)([A-Za-z0-9_-]{43}=)\1[ \t]*(?:\#.*)?$''', text)
    assignments = re.findall(r"(?im)^[ \t]*(?:export[ \t]+)?FERNET_KEY[ \t]*=", text)
    if len(keys) != 1 or len(assignments) != 1:
        raise ValueError("missing or ambiguous Fernet key")
    box, count = Fernet(keys[0][1].encode()), 0
    with closing(sqlite3.connect((work / "data/hub.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        for (token,) in db.execute("SELECT upstream_auth_enc FROM services WHERE upstream_auth_enc IS NOT NULL AND upstream_auth_enc != ''"):
            box.decrypt(token.encode() if isinstance(token, str) else token)
            count += 1
    return count


def verify(archive, identity, temp_dir, seconds=600):
    if not 1 <= seconds <= 3600:
        raise ValueError("invalid deadline")
    with backup.locked_directory(temp_dir, ".verify.lock"):
        backup.cleanup_orphans(temp_dir, ".verify-")
        return verify_locked(archive, identity, temp_dir, seconds)


def verify_locked(archive, identity, temp_dir, seconds):
    """Keep cleanup, capacity checks, decryption and validation inside one lock."""
    backup.regular(archive)
    backup.regular(identity)
    if identity.stat().st_mode & 0o077 or identity.stat().st_uid != os.geteuid():
        raise ValueError("unsafe identity permissions")
    # age ciphertext bounds its decrypted gzip size; extraction has its own cap.
    if archive.stat().st_size > MAX_BYTES:
        raise ValueError("oversized ciphertext")
    backup.require_space(temp_dir, MAX_BYTES + archive.stat().st_size + backup.RESERVE_BYTES)
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=temp_dir) as tmp:
        work = Path(tmp)
        backup.private_dir(work)
        with backup.private_file(work / "decrypted.tar.gz") as stream:
            backup.command(["age", "--decrypt", "-i", str(identity), str(archive)], seconds, stdout=stream)
        members = extract(work / "decrypted.tar.gz", work)
        manifest = json.loads((work / "manifest.json").read_text())
        required = {"data/main.db", "data/hub.db", *("config/" + p for p in backup.ENVS)}
        if manifest["version"] != 1 or members != set(manifest["files"]) or not required <= members:
            raise ValueError("incomplete manifest")
        for member, info in manifest["files"].items():
            path = work / member
            if info["size"] != path.stat().st_size or info["sha256"] != backup.sha256(path):
                raise ValueError("hash mismatch")
        counts = {}
        for label, (_, tables) in backup.DATABASES.items():
            member = "data/" + label + ".db"
            counts[label] = backup.inspect_database(work / member, tables, time.monotonic() + seconds)
            if counts[label] != manifest["files"][member]["counts"]:
                raise ValueError("count mismatch")
        return {"status": "success", "counts": counts, "decrypted_upstream_auth": verify_hub(work)}


def cli():
    parser = backup.Parser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--identity", type=Path, default=Path("/etc/agent-tools-backup/identity.agekey"))
    parser.add_argument("--temp-dir", type=Path, default=Path("/var/backups/agent-tools"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, backup.interrupted)
    try:
        if not 1 <= args.timeout <= 3600:
            raise ValueError()
        print(json.dumps(verify(args.archive.absolute(), args.identity.absolute(), args.temp_dir.absolute(), args.timeout)))
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"status":"failure","error":"verification_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())