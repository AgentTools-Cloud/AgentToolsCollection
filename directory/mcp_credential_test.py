"""Credential test for MCP servers that ask for authentication.

Our daily probe is anonymous, so a server that answers 401 never shows us its
handshake, protocol revision or tools. Anyone signed in (or with an API key)
who has access can send us a credential once: we call the listing's own
endpoint with it (initialize, then tools/list; never a tool), record what came
back and drop the credential. It is not stored, logged or put in an error.

Every test stays on the listing's public record. The latest successful one is
copied to mcp_servers.credential_test_json and scored like our own probe of an
open server (see listing_readiness.credential_test).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from urllib.parse import urljoin, urlsplit

import httpx

from . import crawlers, db, listing_readiness, public_http

UA = "agent-tools.cloud credential-test/1 (+https://agent-tools.cloud/about#mcp-credential-test)"
TIMEOUT = httpx.Timeout(connect=8.0, read=20.0, write=10.0, pool=5.0)
MAX_SECONDS = 45.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
MAX_TOOL_PAGES = 10
MAX_CREDENTIAL = 4096
ACCOUNT_DAILY_LIMIT = 30
DEFAULT_HEADER = "Authorization"
SUCCESS = listing_readiness.CREDENTIAL_TEST_OK
_HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_RESERVED = {
    "host", "content-length", "content-type", "accept", "accept-encoding", "connection",
    "transfer-encoding", "te", "trailer", "upgrade", "cookie", "user-agent", "origin",
    "referer", "expect", "keep-alive", "forwarded", "x-forwarded-for", "x-real-ip",
    "proxy-authorization", "mcp-session-id", "mcp-protocol-version",
}
_REDIRECTS = (301, 302, 303, 307, 308)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_credential_tests (
  id               TEXT PRIMARY KEY,
  server_id        INTEGER NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  user_id          INTEGER NOT NULL,
  created_at       INTEGER NOT NULL,
  endpoint_url     TEXT NOT NULL,
  header_name      TEXT NOT NULL,
  status           TEXT NOT NULL,
  error            TEXT,
  http_status      INTEGER,
  latency_ms       INTEGER,
  protocol_version TEXT,
  tool_count       INTEGER,
  described_count  INTEGER,
  tools_json       TEXT,
  publish_tools    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_credential_tests_server
  ON mcp_credential_tests(server_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mcp_credential_tests_user
  ON mcp_credential_tests(user_id, created_at);
"""


class CredentialTestError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def credential_header(header: str | None, credential: str | None) -> tuple[str, str]:
    """(header name, header value); a bare Authorization token becomes Bearer."""
    name = (header or DEFAULT_HEADER).strip()
    if not _HEADER_RE.fullmatch(name) or name.lower() in _RESERVED:
        raise CredentialTestError(
            "bad_header", "Use Authorization or a custom header name such as X-API-Key")
    value = (credential or "").strip()
    if (not value or len(value) > MAX_CREDENTIAL
            or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value)):
        raise CredentialTestError(
            "bad_credential", "The credential must be 1-4096 printable ASCII characters")
    if name.lower() == "authorization" and " " not in value:
        value = "Bearer " + value
    return name, value


def _origin(url: str) -> tuple:
    p = urlsplit(url)
    scheme = p.scheme.lower()
    return scheme, (p.hostname or "").lower(), p.port or (443 if scheme == "https" else 80)


def _loads(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _pick(msg, want_id):
    if isinstance(msg, list):
        msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == want_id), None)
    return msg if isinstance(msg, dict) and msg.get("id") == want_id else None


def _read_message(response: httpx.Response, want_id, deadline: float):
    """The JSON-RPC reply with `want_id`, from a JSON or SSE body, within limits."""
    size = 0

    def check(n: int) -> None:
        if n > MAX_RESPONSE_BYTES:
            raise CredentialTestError("response_too_large", "Response exceeds the size limit", 502)
        if time.monotonic() > deadline:
            raise CredentialTestError("timeout", "The test exceeded its time limit", 504)

    if "text/event-stream" in (response.headers.get("content-type") or "").lower():
        data: list[str] = []
        for line in response.iter_lines():
            size += len(line) + 1
            check(size)
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                found = _pick(_loads("\n".join(data)), want_id)
                if found:
                    return found
                data = []
        return _pick(_loads("\n".join(data)), want_id) if data else None
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        check(len(body))
    return _pick(_loads(bytes(body)), want_id)


def _post(client: httpx.Client, url: str, payload: dict, headers: dict, deadline: float):
    """POST, following same-origin HTTPS redirects only; returns (status, headers, reply, url)."""
    want_id = payload.get("id")
    for _hop in range(MAX_REDIRECTS + 1):
        with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code in _REDIRECTS:
                target = urljoin(url, response.headers.get("location") or "")
                if _origin(target) != _origin(url):
                    raise CredentialTestError(
                        "redirect_other_origin",
                        "The endpoint redirected to another origin; the credential was not sent there",
                        502)
                url = target
                continue
            reply = None
            if response.status_code < 300 and want_id is not None:
                reply = _read_message(response, want_id, deadline)
            return response.status_code, response.headers, reply, url
    raise CredentialTestError("too_many_redirects", "The endpoint redirected too many times", 502)


def probe(endpoint_url: str, header_name: str, header_value: str,
          client: httpx.Client | None = None) -> dict:
    """initialize + tools/list with the credential header. Never raises."""
    out = {"status": "failed", "error": None, "http_status": None, "latency_ms": None,
           "protocol_version": None, "tool_count": None, "described_count": None, "tools": None}
    if urlsplit(endpoint_url).scheme.lower() != "https":
        return {**out, "error": "not_https"}
    deadline = time.monotonic() + MAX_SECONDS
    headers = {"User-Agent": UA, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", header_name: header_value}
    own = client is None
    try:
        if own:
            public_http.validate_url(endpoint_url)
            client = public_http.client(timeout=TIMEOUT, follow_redirects=False)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": crawlers.MCP_LATEST_REVISION, "capabilities": {},
                           "clientInfo": {"name": "agent-tools.cloud credential test",
                                          "version": "1"}}}
        started = time.monotonic()
        status, reply_headers, reply, url = _post(client, endpoint_url, init, headers, deadline)
        out["latency_ms"] = int((time.monotonic() - started) * 1000)
        out["http_status"] = status
        if status in (401, 403, 407):
            return {**out, "status": "auth_rejected", "error": "credential_not_accepted"}
        if status >= 300:
            return {**out, "error": f"http_{status}"}
        result = reply.get("result") if reply else None
        if not isinstance(result, dict):
            return {**out, "error": "initialize_error" if reply and reply.get("error") else "not_mcp"}
        revision = str(result.get("protocolVersion") or "")[:32] or None
        out["protocol_version"] = revision
        session = dict(headers)
        sid = reply_headers.get("mcp-session-id")
        if sid:
            session["Mcp-Session-Id"] = sid
        if revision:
            session["MCP-Protocol-Version"] = revision
        try:
            _post(client, url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  session, deadline)
        except httpx.HTTPError:
            pass
        tools: list[dict] = []
        listed = False
        cursor = None
        for page in range(MAX_TOOL_PAGES):
            request = {"jsonrpc": "2.0", "id": 2 + page, "method": "tools/list",
                       "params": {"cursor": cursor} if cursor else {}}
            code, _h, page_reply, url = _post(client, url, request, session, deadline)
            page_result = page_reply.get("result") if page_reply else None
            if code >= 300 or not isinstance(page_result, dict) \
                    or not isinstance(page_result.get("tools"), list):
                break
            listed = True
            tools.extend(t for t in page_result["tools"]
                         if isinstance(t, dict) and str(t.get("name") or "").strip())
            cursor = page_result.get("nextCursor")
            if not cursor:
                break
        if sid:
            try:
                client.request("DELETE", url, headers={k: v for k, v in session.items()
                                                       if k != "Content-Type"})
            except httpx.HTTPError:
                pass
        if not listed:
            return {**out, "status": "partial", "error": "tools_list_failed"}
        return {**out, "status": "succeeded", "tool_count": len(tools),
                "described_count": sum(1 for t in tools if str(t.get("description") or "").strip()),
                "tools": crawlers._summarize_tools(tools)}
    except public_http.UnsafeURL:
        return {**out, "error": "unsafe_url"}
    except CredentialTestError as exc:
        return {**out, "error": exc.code}
    except httpx.TimeoutException:
        return {**out, "error": "timeout"}
    except httpx.HTTPError:
        return {**out, "error": "unreachable"}
    finally:
        if own and client is not None:
            client.close()


def _public(row) -> dict:
    d = dict(row)
    tools = _loads(d.get("tools_json")) if d.get("tools_json") else None
    return {"id": d["id"], "tested_at": d["created_at"], "status": d["status"],
            "error": d["error"], "http_status": d["http_status"], "latency_ms": d["latency_ms"],
            "protocol_version": d["protocol_version"], "tool_count": d["tool_count"],
            "described_count": d["described_count"], "tools_public": bool(d["publish_tools"]),
            "tools": tools if d["publish_tools"] else None, "header": d["header_name"],
            "endpoint_url": d["endpoint_url"]}


def history(conn, server_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mcp_credential_tests WHERE server_id=? ORDER BY created_at DESC, rowid DESC",
        (int(server_id),)).fetchall()
    return [_public(r) for r in rows]


def _rescore(conn, server_id: int) -> None:
    row = dict(conn.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone())
    report = listing_readiness.for_mcp_row(conn, row)
    conn.execute(
        "UPDATE mcp_servers SET quality_score=?, readiness_score=?, readiness_grade=?, "
        "readiness_checked=? WHERE id=?",
        (db.mcp_quality_score(row), report["score"], report["grade"], int(time.time()), server_id))


def run(user_id: int, server: dict, credential: str | None, header: str | None = None,
        publish_tools: bool = True, client: httpx.Client | None = None) -> dict:
    name, value = credential_header(header, credential)
    endpoint = (server.get("endpoint_url") or "").strip()
    if not endpoint:
        raise CredentialTestError("no_endpoint", "This listing has no remote endpoint", 409)
    since = int(time.time()) - 86400
    with db.connect(read_only=True) as conn:
        used = conn.execute(
            "SELECT COUNT(*) FROM mcp_credential_tests WHERE user_id=? AND created_at>=?",
            (int(user_id), since)).fetchone()[0]
    if used >= ACCOUNT_DAILY_LIMIT:
        raise CredentialTestError(
            "rate_limited", f"At most {ACCOUNT_DAILY_LIMIT} credential tests per account per day", 429)
    result = probe(endpoint, name, value, client=client)
    del value
    now = int(time.time())
    tid = uuid.uuid4().hex
    tools = result.get("tools")
    record = (tid, int(server["id"]), int(user_id), now, endpoint, name, result["status"],
              result["error"], result["http_status"], result["latency_ms"],
              result["protocol_version"], result["tool_count"], result["described_count"],
              json.dumps(tools, ensure_ascii=False) if publish_tools and tools is not None else None,
              int(bool(publish_tools)))
    with db.writer(immediate=True) as conn:
        conn.execute(
            "INSERT INTO mcp_credential_tests(id,server_id,user_id,created_at,endpoint_url,"
            "header_name,status,error,http_status,latency_ms,protocol_version,tool_count,"
            "described_count,tools_json,publish_tools) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            record)
        if result["status"] in SUCCESS:
            summary = {"id": tid, "at": now, "status": result["status"], "endpoint_url": endpoint,
                       "http_status": result["http_status"], "latency_ms": result["latency_ms"],
                       "protocol_version": result["protocol_version"],
                       "tool_count": result["tool_count"], "described_count": result["described_count"]}
            changed = conn.execute(
                "UPDATE mcp_servers SET credential_test_json=? WHERE id=? AND endpoint_url=?",
                (json.dumps(summary, separators=(",", ":")), int(server["id"]), endpoint)).rowcount
            if changed:
                _rescore(conn, int(server["id"]))
        row = conn.execute("SELECT * FROM mcp_credential_tests WHERE id=?", (tid,)).fetchone()
    return _public(row)
