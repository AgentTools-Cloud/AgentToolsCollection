"""Hand the on-chain demand measurement to a worker host, then import it here.

The measurement is network-bound (RPC rate limits) and takes hours; the site
host is small. With AGENT_TOOLS_ONCHAIN_WORKER=<ssh target> the nightly
`jobs onchain`:
  1. plans here (onchain_plan: which addresses, which listings),
  2. writes the plan to data/onchain-remote/<batch>/plan.json,
  3. sends the address list to <worker>:<dir>/inbox/<batch>.keys.json and
     starts agent-tools-onchain-worker.service there.
The worker (`jobs onchain-worker`) measures and writes
outbox/<batch>.results.jsonl.gz, then outbox/<batch>.manifest.json last.
This host pulls them (`jobs onchain-import`, every few minutes), checks the
manifest and applies them with the same OnchainApplier the local path uses.
Only this host writes the database.

Fallbacks: if handing over fails, or a batch has been out longer than
PENDING_MAX_S, the nightly run measures locally as before.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import time

from . import db

log = logging.getLogger("directory.onchain_remote")

WORKER_DIR = os.environ.get("AGENT_TOOLS_ONCHAIN_WORKER_DIR", "/opt/agent-tools-worker")
WORKER_UNIT = "agent-tools-onchain-worker.service"
PENDING_MAX_S = 30 * 3600
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def worker() -> str | None:
    return (os.environ.get("AGENT_TOOLS_ONCHAIN_WORKER") or "").strip() or None


def local_dir() -> str:
    return os.path.join(os.path.dirname(db.DEFAULT_DB_PATH) or ".", "onchain-remote")


def _ssh(cmd: str, data: bytes | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", worker(), cmd],
        input=data, capture_output=True, timeout=timeout, check=True)


def _code_sha() -> str:
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _pending() -> list[tuple[str, str, dict]]:
    """(batch, dir, meta) for batches sent out and not yet imported, oldest first."""
    root = local_dir()
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        meta_path = os.path.join(path, "batch.json")
        if (not name.startswith("b") or not os.path.isfile(meta_path)
                or os.path.exists(os.path.join(path, "done"))):
            continue
        with open(meta_path) as fh:
            out.append((name, path, json.load(fh)))
    return out


def _close(path: str, outcome: str) -> None:
    with open(os.path.join(path, "done"), "w") as fh:
        fh.write(f"{outcome} {int(time.time())}\n")


def dispatch(plan: dict) -> bool:
    """Send a plan to the worker. True: handled there (or one still running)."""
    now = int(time.time())
    for batch, path, meta in _pending():
        if now - meta["sent"] < PENDING_MAX_S:
            log.info("onchain: batch %s still out on the worker; not sending another", batch)
            return True
        log.warning("onchain: batch %s out for %.1f h without results; measuring here",
                    batch, (now - meta["sent"]) / 3600)
        _close(path, "abandoned")
        return False
    batch = "b" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    path = os.path.join(local_dir(), batch)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "plan.json"), "w") as fh:
        json.dump(plan, fh)
    keys = {"batch": batch, "code": _code_sha(), "created": now, "work": plan["work"]}
    d = shlex.quote(WORKER_DIR)
    _ssh(f"mkdir -p {d}/inbox {d}/outbox && cat > {d}/inbox/{batch}.keys.json.tmp && "
         f"mv {d}/inbox/{batch}.keys.json.tmp {d}/inbox/{batch}.keys.json",
         json.dumps(keys).encode())
    _ssh(f"systemctl start --no-block {WORKER_UNIT}")
    with open(os.path.join(path, "batch.json"), "w") as fh:
        json.dump({"batch": batch, "sent": now, "code": keys["code"],
                   "keys": len(plan["work"])}, fh)
    log.info("onchain: sent batch %s (%d addresses) to %s", batch, len(plan["work"]), worker())
    return True


# --- worker side ------------------------------------------------------------

def _encode(results: dict):
    for (chain, address), res in results.items():
        row = {"chain": chain, "address": address}
        for k, v in (res or {}).items():
            row[k] = sorted(v) if isinstance(v, (set, frozenset)) else v
        yield json.dumps(row, separators=(",", ":"), default=str) + "\n"


def cmd_worker(keys_path: str, outbox: str) -> int:
    from . import jobs
    with open(keys_path) as fh:
        keys = json.load(fh)
    batch = keys["batch"]
    results = jobs.onchain_measure(keys["work"])
    os.makedirs(outbox, exist_ok=True)
    final = os.path.join(outbox, f"{batch}.results.jsonl.gz")
    with gzip.open(final + ".tmp", "wt") as fh:
        fh.writelines(_encode(results))
    os.replace(final + ".tmp", final)
    with open(final, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    manifest = {"batch": batch, "code": keys.get("code"), "rows": len(results),
                "keys": len(keys["work"]), "sha256": digest, "finished": int(time.time())}
    man = os.path.join(outbox, f"{batch}.manifest.json")
    with open(man + ".tmp", "w") as fh:
        json.dump(manifest, fh)
    os.replace(man + ".tmp", man)
    os.unlink(keys_path)
    log.info("onchain worker: batch %s done, %d results", batch, len(results))
    return 0


# --- import side ------------------------------------------------------------

class ImportError_(RuntimeError):
    pass


def load_results(path: str, manifest: dict, plan: dict) -> dict:
    with open(path, "rb") as fh:
        if hashlib.sha256(fh.read()).hexdigest() != manifest.get("sha256"):
            raise ImportError_("checksum mismatch")
    wanted = {tuple(k) for k in plan["work"]}
    results = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            row = json.loads(line)
            key = (row.pop("chain"), row.pop("address"))
            if key not in wanted:
                raise ImportError_(f"result for an address not in the plan: {key}")
            if not isinstance(row.get("ok"), bool):
                raise ImportError_("result without ok flag")
            for k in ("tx", "payers", "raw_tx"):
                if row.get(k) is not None and (not isinstance(row[k], int) or row[k] < 0):
                    raise ImportError_(f"bad {k} for {key}")
            results[key] = row
    if len(results) != manifest.get("rows"):
        raise ImportError_("row count differs from the manifest")
    return results


def cmd_import() -> int:
    from . import jobs
    pending = _pending()
    if not pending:
        return 0
    if not worker():
        log.warning("onchain-import: %d batch(es) pending but no worker configured", len(pending))
        return 0
    d = WORKER_DIR
    for batch, path, meta in pending:
        try:
            got = _ssh(f"cat {shlex.quote(d)}/outbox/{batch}.manifest.json 2>/dev/null || true")
        except Exception as exc:  # noqa: BLE001
            log.warning("onchain-import: worker unreachable: %r", exc)
            return 0
        if not got.stdout.strip():
            continue        # still measuring
        manifest = json.loads(got.stdout)
        if manifest.get("batch") != batch:
            log.error("onchain-import: manifest for %s names %s", batch, manifest.get("batch"))
            _close(path, "rejected")
            continue
        local = os.path.join(path, "results.jsonl.gz")
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                        f"{worker()}:{d}/outbox/{batch}.results.jsonl.gz", local + ".tmp"],
                       check=True, timeout=600)
        os.replace(local + ".tmp", local)
        with open(os.path.join(path, "plan.json")) as fh:
            plan = json.load(fh)
        try:
            results = load_results(local, manifest, plan)
        except ImportError_ as exc:
            log.error("onchain-import: batch %s rejected: %s", batch, exc)
            _close(path, "rejected")
            continue
        if manifest.get("code") and manifest.get("code") != meta.get("code"):
            log.warning("onchain-import: batch %s measured with code %s, sent with %s",
                        batch, manifest.get("code"), meta.get("code"))
        apply = jobs.OnchainApplier(plan)
        apply(results)
        log.info("onchain-import: batch %s: %d results, wrote %d services (%d with paying "
                 "demand, %d changed since the plan)", batch, len(results),
                 apply.counts["written"], apply.counts["active"], apply.counts["changed"])
        _close(path, "imported")
        try:
            _ssh(f"rm -f {shlex.quote(d)}/outbox/{batch}.results.jsonl.gz "
                 f"{shlex.quote(d)}/outbox/{batch}.manifest.json")
        except Exception:  # noqa: BLE001 - leftovers are harmless
            log.warning("onchain-import: could not remove batch %s on the worker", batch)
        os.unlink(local)
        jobs.cmd_rescore()
    # keep the last 10 closed batch directories for inspection
    root = local_dir()
    closed = sorted(n for n in os.listdir(root)
                    if os.path.exists(os.path.join(root, n, "done")))
    for name in closed[:-10]:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    return 0
