"""Seller-declared call interfaces from the x402 v2 ``bazaar`` extension.

A resource server may attach ``extensions.bazaar`` to its 402 response
(specs/extensions/bazaar.md in coinbase/x402): ``info.input`` says how to call
the resource (HTTP method with example query parameters or body, or an MCP tool
name with its input schema) and ``info.output`` gives an example response. The
CDP Bazaar discovery feed republishes the same object per resource.

The data is untrusted seller metadata. ``extract`` keeps only fields the
specification defines, bounds every size, applies the spec's soft-drop rules to
``routeTemplate`` and validates ``info`` against the bundled ``schema`` when it
has no external references. Anything that fails is dropped, never repaired.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from .url_identity import exact_endpoint

QUERY_METHODS = ("GET", "HEAD", "DELETE")
BODY_METHODS = ("POST", "PUT", "PATCH")
BODY_TYPES = ("json", "form-data", "text")
MCP_TRANSPORTS = ("streamable-http", "sse")

MAX_MAP_ENTRIES = 40
MAX_KEY = 128
MAX_VALUE = 1024
MAX_BODY_BYTES = 16 * 1024
MAX_SCHEMA_BYTES = 32 * 1024
MAX_EXAMPLE_BYTES = 8 * 1024
MAX_TEXT = 600
# Probes send a declared body only when it is small.
MAX_PROBE_BODY_BYTES = 4 * 1024
# Declarations kept per listing (listing URL plus its resource samples).
MAX_PER_LISTING = 50

_ROUTE_RE = re.compile(r"^/[a-zA-Z0-9_/:.\-~%]+$")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Header names/values and query/path examples: no line breaks either.
_CTRL_LINE = re.compile(r"[\x00-\x1f\x7f]")


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or _CTRL.search(value):
        return None
    return value


def _encoded(value: Any) -> str | None:
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None


def _bounded(value: Any, limit: int) -> Any:
    encoded = _encoded(value)
    if encoded is None or len(encoded) > limit:
        return None
    return json.loads(encoded)


def _string_map(value: Any) -> dict[str, str] | None:
    """Example query parameters / headers / path parameters: str -> scalar."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_MAP_ENTRIES:
        return None
    out: dict[str, str] = {}
    for key, item in value.items():
        key = _text(key, MAX_KEY)
        if key is None or _CTRL_LINE.search(key):
            return None
        if isinstance(item, bool):
            item = "true" if item else "false"
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            item = str(item)
        if not isinstance(item, str) or len(item) > MAX_VALUE or _CTRL_LINE.search(item):
            return None
        out[key] = item
    return out


def valid_route_template(value: Any) -> str | None:
    """The spec's routeTemplate rules (percent-decoded before the checks)."""
    if not isinstance(value, str) or not value or not _ROUTE_RE.match(value):
        return None
    decoded = unquote(value)
    if ".." in decoded or "://" in decoded:
        return None
    return value


def _has_external_ref(schema: Any) -> bool:
    if isinstance(schema, dict):
        for key, item in schema.items():
            if key in ("$ref", "$id", "$dynamicRef") and not (
                    isinstance(item, str) and item.startswith("#")):
                return True
            if _has_external_ref(item):
                return True
    elif isinstance(schema, list):
        return any(_has_external_ref(item) for item in schema)
    return False


def _schema_accepts(info: dict, schema: Any) -> bool:
    """Validate ``info`` against the bundled schema without resolving refs.

    A missing schema is tolerated (the CDP feed sometimes strips it); a schema
    that is oversized, references external documents, is itself invalid, or
    rejects ``info`` drops the declaration.
    """
    if schema is None:
        return True
    if not isinstance(schema, dict) or _has_external_ref(schema):
        return False
    encoded = _encoded(schema)
    if encoded is None or len(encoded) > MAX_SCHEMA_BYTES:
        return False
    try:
        import jsonschema
        from jsonschema.validators import validator_for
    except ImportError:  # pragma: no cover - the app venv ships jsonschema
        return True
    try:
        cls = validator_for(schema, default=jsonschema.Draft202012Validator)
        cls.check_schema(schema)
        return cls(schema).is_valid(info)
    except Exception:  # noqa: BLE001 - hostile schemas must never raise here
        return False


def _extension(obj: Any) -> dict | None:
    """Find the bazaar extension object in a 402 body, a discovery item, or
    the extension itself."""
    if not isinstance(obj, dict):
        return None
    ext = obj.get("extensions")
    if isinstance(ext, dict) and isinstance(ext.get("bazaar"), dict):
        return ext["bazaar"]
    if isinstance(obj.get("info"), dict) and isinstance(obj["info"].get("input"), dict):
        return obj
    return None


def _output(info: dict) -> dict | None:
    output = info.get("output")
    if not isinstance(output, dict):
        return None
    out: dict[str, Any] = {}
    kind = _text(output.get("type"), 64)
    if kind:
        out["type"] = kind
    fmt = _text(output.get("format"), 128)
    if fmt:
        out["format"] = fmt
    if "example" in output:
        example = _bounded(output["example"], MAX_EXAMPLE_BYTES)
        if example is not None:
            out["example"] = example
    return out or None


def extract(obj: Any) -> dict | None:
    """Normalized, validated declaration, or None."""
    ext = _extension(obj)
    if ext is None:
        return None
    info = ext.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("input"), dict):
        return None
    if not _schema_accepts(info, ext.get("schema")):
        return None
    raw = info["input"]
    kind = raw.get("type")
    decl: dict[str, Any]
    if kind == "http":
        method = str(raw.get("method") or "").upper()
        if method not in QUERY_METHODS + BODY_METHODS:
            return None
        decl = {"type": "http", "method": method}
        for src, dst in (("queryParams", "query_params"), ("headers", "headers"),
                         ("pathParams", "path_params")):
            mapped = _string_map(raw.get(src))
            if mapped is None:
                return None
            if mapped:
                decl[dst] = mapped
        if method in BODY_METHODS:
            body_type = raw.get("bodyType")
            if body_type not in BODY_TYPES or "body" not in raw:
                return None
            body = raw["body"]
            if body_type == "json" and not isinstance(body, (dict, list)):
                return None
            if not isinstance(body, (dict, list, str)):
                return None
            body = _bounded(body, MAX_BODY_BYTES)
            if body is None:
                return None
            decl["body_type"] = body_type
            decl["body"] = body
        elif "body" in raw or "bodyType" in raw:
            return None
    elif kind == "mcp":
        tool = _text(raw.get("toolName"), 256)
        schema = raw.get("inputSchema")
        if tool is None or not isinstance(schema, dict):
            return None
        schema = _bounded(schema, MAX_SCHEMA_BYTES)
        if schema is None:
            return None
        decl = {"type": "mcp", "tool_name": tool, "input_schema": schema}
        transport = raw.get("transport")
        if transport is not None:
            if transport not in MCP_TRANSPORTS:
                return None
            decl["transport"] = transport
        description = _text(raw.get("description"))
        if description:
            decl["description"] = description
        if "example" in raw:
            example = _bounded(raw["example"], MAX_BODY_BYTES)
            if isinstance(example, dict):
                decl["example"] = example
    else:
        return None
    route = valid_route_template(ext.get("routeTemplate"))
    if route:
        decl["route_template"] = route
    output = _output(info)
    if output:
        decl["output"] = output
    return decl


def key(url: str) -> str:
    return exact_endpoint(str(url or "").strip())


def for_url(row: dict, url: str) -> dict | None:
    """The declaration stored on a listing for this exact URL."""
    stored = row.get("declared") if isinstance(row, dict) else None
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except (TypeError, ValueError):
            return None
    if not isinstance(stored, dict) or not url:
        return None
    entry = stored.get(key(url))
    decl = entry.get("decl") if isinstance(entry, dict) else None
    return decl if isinstance(decl, dict) else None


def probe_body(decl: dict | None) -> dict | list | None:
    """A declared JSON body small enough to send in one unpaid probe."""
    if not isinstance(decl, dict) or decl.get("type") != "http":
        return None
    if decl.get("method") != "POST" or decl.get("body_type") != "json":
        return None
    body = decl.get("body")
    encoded = _encoded(body)
    if not isinstance(body, (dict, list)) or encoded is None or len(encoded) > MAX_PROBE_BODY_BYTES:
        return None
    return body


def query_url(url: str, decl: dict | None) -> str:
    """URL with the declared example query appended when the URL has none."""
    if not isinstance(decl, dict) or decl.get("type") != "http":
        return url
    params = decl.get("query_params") or {}
    if not params or not url:
        return url
    parts = urlsplit(url)
    if parts.query:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def merge(stored: Any, found: dict[str, dict], source: str, now: int) -> dict:
    """Merge newly seen declarations into a listing's stored map.

    A declaration read from our own probe of the resource ("challenge") is
    first-hand and is not replaced by a feed copy ("cdp-bazaar") for 30 days.
    """
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except (TypeError, ValueError):
            stored = {}
    out = dict(stored) if isinstance(stored, dict) else {}
    for url_key, decl in found.items():
        if not url_key or not isinstance(decl, dict):
            continue
        current = out.get(url_key)
        if (source != "challenge" and isinstance(current, dict)
                and current.get("source") == "challenge"
                and now - int(current.get("seen") or 0) < 30 * 86400):
            continue
        out[url_key] = {"decl": decl, "source": source, "seen": int(now)}
    if len(out) > MAX_PER_LISTING:
        keep = sorted(out.items(), key=lambda kv: -int(kv[1].get("seen") or 0))[:MAX_PER_LISTING]
        out = dict(keep)
    return out


def listing_keys(row: dict) -> set[str]:
    """Exact URLs a listing owns: its endpoint plus its resource samples."""
    keys = set()
    if row.get("url"):
        keys.add(key(row["url"]))
    samples = row.get("resource_samples")
    if isinstance(samples, str):
        try:
            samples = json.loads(samples)
        except (TypeError, ValueError):
            samples = []
    if not samples:
        info = row.get("call_info")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except (TypeError, ValueError):
                info = {}
        samples = (info or {}).get("resource_samples") if isinstance(info, dict) else []
    for sample in samples or []:
        if isinstance(sample, dict):
            u = sample.get("url") or sample.get("resource")
            if isinstance(u, str) and u:
                keys.add(key(u))
    keys.discard("")
    return keys


def public(decl: dict | None) -> dict | None:
    """What cards and pages show (the stored form is already public-safe)."""
    return dict(decl) if isinstance(decl, dict) else None
