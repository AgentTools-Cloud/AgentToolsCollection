"""Is a code-only MCP listing actually an MCP server?

Repositories and packages tagged ``mcp-server`` include many things that are
not servers: MCP clients and agent hosts, SDKs and frameworks for building
servers, awesome-lists, tutorials, agent skills and unrelated apps. We only
list servers, so every code-only row is classified once by the language model
from its name, description and topics, in batches.

Verdicts are kept in ``code_mcp_class`` keyed by (source, source_id) and
the content they were made from, so the daily crawl only asks about new or
changed rows. Rows judged not to be servers are not listed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import httpx

from . import db

log = logging.getLogger(__name__)

CLASSES = ("server", "client", "sdk", "list", "skill", "other")
KEEP = {"server"}
BATCH = 40
PROMPT_VERSION = 1

SYSTEM = """You classify software projects that are tagged "mcp-server" (Model Context Protocol).
For each item decide what the project IS, from its name, description and topics:
- server: it ships an MCP server a user can install and run (including an app or tool whose
  package provides an MCP server mode or component as a real feature).
- client: an MCP client, agent, IDE, chat app or host that CONNECTS TO MCP servers, with no
  server of its own.
- sdk: a library, SDK, framework, template, generator, proxy/gateway toolkit or test tool for
  BUILDING or running MCP servers in general (not one server with its own tools).
- list: an awesome-list, directory, catalogue, tutorial, course, book, docs or examples collection.
- skill: agent skills / prompts / rules packs, not an MCP server.
- other: unrelated, empty, placeholder or unclear with no sign of an MCP server.
When an item is both (e.g. an app that also exposes an MCP server), answer server.
Answer ONLY a JSON object {"<id>": "<class>", ...} covering every id."""


def _backend():
    from .a2a_agent import _backend as backend
    return backend()


def configured() -> bool:
    base, key, model = _backend()
    return bool(base and key and model)


def _tags(row: dict) -> list:
    tags = row.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = [tags]
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _digest(row: dict) -> str:
    parts = [row.get("name") or "", row.get("description") or "", ",".join(_tags(row))]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _item_text(i: int, row: dict) -> str:
    tags = _tags(row)
    desc = (row.get("description") or "").replace("\n", " ")[:300]
    where = row.get("source_code_url") or row.get("package_name") or ""
    return json.dumps({"id": str(i), "name": row.get("name"), "where": where,
                       "description": desc, "topics": (tags or [])[:8]}, ensure_ascii=False)


def _ask(rows: list[dict]) -> dict[int, str]:
    base, key, model = _backend()
    user = "\n".join(_item_text(i, r) for i, r in enumerate(rows))
    body = {"model": model, "temperature": 0, "max_tokens": 40 * len(rows) + 200,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    for attempt in range(4):
        try:
            r = httpx.post(base + "/v1/chat/completions", json=body, timeout=180,
                           headers={"Authorization": f"Bearer {key}", "User-Agent": "agent-tools-classify"})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
            start, end = text.find("{"), text.rfind("}")
            data = json.loads(text[start:end + 1])
            out = {}
            for k, v in data.items():
                v = str(v).strip().lower()
                if k.isdigit() and v in CLASSES:
                    out[int(k)] = v
            return out
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            log.warning("code classify: batch failed (%r), attempt %d", e, attempt + 1)
            time.sleep(10 * (attempt + 1))
    return {}


def ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS code_mcp_class ("
        " source TEXT NOT NULL, source_id TEXT NOT NULL, digest TEXT NOT NULL,"
        " class TEXT NOT NULL, version INTEGER NOT NULL, checked_at INTEGER NOT NULL,"
        " PRIMARY KEY (source, source_id))")


def known(conn) -> dict[tuple[str, str], tuple[str, str]]:
    """(source, source_id) -> (digest, class) for the current prompt version."""
    try:
        return {(s, sid): (d, c) for s, sid, d, c in conn.execute(
            "SELECT source, source_id, digest, class FROM code_mcp_class WHERE version=?",
            (PROMPT_VERSION,))}
    except Exception:  # noqa: BLE001 - table not created yet
        return {}


def classify(rows: list[dict], *, workers: int = 4) -> dict[tuple[str, str], str]:
    """Class for every row; asks the model only about rows without a current
    verdict for the same content. Unanswered rows are left out (treated as
    unknown by the caller: kept out of the listing until classified)."""
    from concurrent.futures import ThreadPoolExecutor

    with db.connect(read_only=True) as c:
        have = known(c)
    out: dict[tuple[str, str], str] = {}
    todo = []
    for r in rows:
        k = (r["source"], r["source_id"])
        d = _digest(r)
        if k in have and have[k][0] == d:
            out[k] = have[k][1]
        else:
            todo.append((k, d, r))
    if not todo:
        return out
    if not configured():
        log.warning("code classify: no model configured; %d rows unclassified", len(todo))
        return out
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

    def run(batch):
        got = _ask([r for _k, _d, r in batch])
        return [(k, d, got[i]) for i, (k, d, _r) in enumerate(batch) if i in got]

    now = int(time.time())
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(run, batches):
            if not res:
                continue

            def op(res=res):
                with db.writer() as c:
                    ensure_table(c)
                    c.executemany(
                        "INSERT INTO code_mcp_class(source, source_id, digest, class, version, checked_at) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(source, source_id) DO UPDATE SET "
                        "digest=excluded.digest, class=excluded.class, version=excluded.version, "
                        "checked_at=excluded.checked_at",
                        [(k[0], k[1], d, cls, PROMPT_VERSION, now) for k, d, cls in res])
            db.with_retry(op)
            for k, _d, cls in res:
                out[k] = cls
            done += len(res)
    log.info("code classify: asked %d, answered %d, cached %d", len(todo), done, len(rows) - len(todo))
    return out
