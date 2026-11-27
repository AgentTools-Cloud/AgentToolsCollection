import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from directory import bazaar, cards, crawlers, db, jobs

ENDPOINT = "https://seller.example.test/api/search"
ASSET = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
PAY_TO = "0x1111111111111111111111111111111111111111"


def _ext(input_, output=None, schema=None, route=None):
    info = {"input": input_}
    if output is not None:
        info["output"] = output
    ext = {"info": info}
    if schema is not None:
        ext["schema"] = schema
    if route is not None:
        ext["routeTemplate"] = route
    return ext


POST_EXT = _ext(
    {"type": "http", "method": "POST", "bodyType": "json",
     "body": {"query": "weather in Paris", "limit": 3}},
    output={"type": "json", "example": {"results": []}},
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"input": {"type": "object", "properties": {
            "type": {"const": "http"}, "method": {"enum": ["POST"]}},
            "required": ["type", "method"]}},
        "required": ["input"],
    },
)
GET_EXT = _ext({"type": "http", "method": "GET", "queryParams": {"q": "btc", "n": 5}})


class ExtractTests(unittest.TestCase):
    def test_post_declaration_from_402_body(self):
        decl = bazaar.extract({"x402Version": 2, "extensions": {"bazaar": POST_EXT}})
        self.assertEqual(decl["method"], "POST")
        self.assertEqual(decl["body_type"], "json")
        self.assertEqual(decl["body"], {"query": "weather in Paris", "limit": 3})
        self.assertEqual(decl["output"], {"type": "json", "example": {"results": []}})

    def test_get_query_params_are_strings(self):
        decl = bazaar.extract({"extensions": {"bazaar": GET_EXT}})
        self.assertEqual(decl, {"type": "http", "method": "GET",
                                "query_params": {"q": "btc", "n": "5"}})

    def test_extension_object_itself_is_accepted(self):
        self.assertEqual(bazaar.extract(GET_EXT)["method"], "GET")

    def test_schema_rejecting_info_drops_declaration(self):
        ext = dict(POST_EXT, schema={"type": "object", "properties": {
            "input": {"properties": {"method": {"enum": ["GET"]}}}}})
        self.assertIsNone(bazaar.extract(ext))

    def test_external_ref_schema_is_not_resolved(self):
        ext = dict(GET_EXT, schema={"$ref": "https://evil.example/schema.json"})
        self.assertIsNone(bazaar.extract(ext))
        local = dict(GET_EXT, schema={"$defs": {"o": {"type": "object"}}, "$ref": "#/$defs/o"})
        self.assertIsNotNone(bazaar.extract(local))

    def test_invalid_shapes_are_dropped(self):
        bad = [
            {"type": "http", "method": "TRACE"},
            {"type": "http", "method": "GET", "body": {"a": 1}},
            {"type": "http", "method": "POST", "bodyType": "json"},
            {"type": "http", "method": "POST", "bodyType": "json", "body": "text"},
            {"type": "http", "method": "POST", "bodyType": "xml", "body": {}},
            {"type": "http", "method": "GET", "queryParams": {"q": {"nested": 1}}},
            {"type": "http", "method": "GET", "headers": {"x": "a\nb"}},
            {"type": "ftp"},
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                self.assertIsNone(bazaar.extract(_ext(raw)))

    def test_oversized_body_is_dropped(self):
        raw = {"type": "http", "method": "POST", "bodyType": "json",
               "body": {"blob": "x" * (bazaar.MAX_BODY_BYTES + 10)}}
        self.assertIsNone(bazaar.extract(_ext(raw)))

    def test_route_template_soft_drop(self):
        ok = bazaar.extract(_ext({"type": "http", "method": "GET"}, route="/users/:id"))
        self.assertEqual(ok["route_template"], "/users/:id")
        for route in ("/a/%2e%2e/b", "/x/http://y", "no-slash"):
            with self.subTest(route=route):
                decl = bazaar.extract(_ext({"type": "http", "method": "GET"}, route=route))
                self.assertIsNotNone(decl)
                self.assertNotIn("route_template", decl)

    def test_mcp_declaration(self):
        decl = bazaar.extract(_ext({
            "type": "mcp", "toolName": "search", "transport": "sse",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
            "example": {"q": "hi"}}))
        self.assertEqual(decl["type"], "mcp")
        self.assertEqual(decl["tool_name"], "search")
        self.assertEqual(decl["transport"], "sse")
        self.assertEqual(decl["example"], {"q": "hi"})
        self.assertIsNone(bazaar.extract(_ext({
            "type": "mcp", "toolName": "s", "transport": "stdio", "inputSchema": {}})))

    def test_probe_body_and_query_url(self):
        post = bazaar.extract(POST_EXT)
        self.assertEqual(bazaar.probe_body(post), post["body"])
        big = dict(post, body={"blob": "x" * (bazaar.MAX_PROBE_BODY_BYTES + 1)})
        self.assertIsNone(bazaar.probe_body(big))
        get = bazaar.extract(GET_EXT)
        self.assertEqual(bazaar.query_url(ENDPOINT, get), ENDPOINT + "?q=btc&n=5")
        self.assertEqual(bazaar.query_url(ENDPOINT + "?a=1", get), ENDPOINT + "?a=1")


class MergeTests(unittest.TestCase):
    def test_challenge_beats_feed_for_thirty_days(self):
        k = bazaar.key(ENDPOINT)
        first = bazaar.merge(None, {k: {"type": "http", "method": "POST"}}, "challenge", 1000)
        kept = bazaar.merge(first, {k: {"type": "http", "method": "GET"}}, "cdp-bazaar", 2000)
        self.assertEqual(kept[k]["decl"]["method"], "POST")
        later = bazaar.merge(first, {k: {"type": "http", "method": "GET"}}, "cdp-bazaar",
                             1000 + 31 * 86400)
        self.assertEqual(later[k]["decl"]["method"], "GET")

    def test_entries_are_capped(self):
        found = {f"https://h.test/{i}": {"type": "http", "method": "GET"}
                 for i in range(bazaar.MAX_PER_LISTING + 5)}
        self.assertEqual(len(bazaar.merge({}, found, "cdp-bazaar", 1)), bazaar.MAX_PER_LISTING)


def _row_with(decl, url=ENDPOINT):
    return {"url": url, "declared": {bazaar.key(url): {"decl": decl, "source": "challenge", "seen": 1}}}


class CardTests(unittest.TestCase):
    def test_declared_post_sets_method_and_curl_body(self):
        decl = bazaar.extract(POST_EXT)
        row = _row_with(decl)
        request = cards.primary_http_request(row)
        self.assertEqual((request["method"], request["method_known"]), ("POST", True))
        self.assertFalse(request["empty_post_probe"])
        tpl = cards.build_call_template(row)["http_x402"]
        tokens = shlex.split(tpl["curl_probe"], comments=True)
        self.assertEqual(tokens[tokens.index("-X") + 1], "POST")
        self.assertEqual(json.loads(tokens[tokens.index("--data") + 1]), decl["body"])
        self.assertIn("seller declares POST", tpl["method_hint"])
        self.assertEqual(tpl["declared"], decl)

    def test_declared_get_adds_example_query(self):
        row = _row_with(bazaar.extract(GET_EXT))
        tpl = cards.build_call_template(row)["http_x402"]
        self.assertIn("'" + ENDPOINT + "?q=btc&n=5'", tpl["curl_probe"])
        self.assertEqual(tpl["url"], ENDPOINT)

    def test_owner_verified_interface_outranks_declaration(self):
        row = _row_with(bazaar.extract(POST_EXT))
        row["call_info"] = {"primary_url": ENDPOINT, "method": "GET"}
        self.assertEqual(cards.primary_http_request(row)["method"], "GET")
        tpl = cards.build_call_template(row)["http_x402"]
        self.assertNotIn("--data", tpl["curl_probe"])

    def test_declaration_for_another_url_is_ignored(self):
        row = _row_with(bazaar.extract(POST_EXT), url="https://seller.example.test/other")
        row["url"] = ENDPOINT
        self.assertIsNone(cards.primary_http_request(row)["declared"])

    def test_public_row_hides_raw_map(self):
        self.assertNotIn("declared", cards.public_service_row(_row_with(bazaar.extract(GET_EXT))))


def _challenge(extensions=None):
    body = {"x402Version": 2, "resource": {"url": ENDPOINT},
            "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "2000",
                         "asset": ASSET, "payTo": PAY_TO}]}
    if extensions:
        body["extensions"] = extensions
    return body


class ProbeTests(unittest.TestCase):
    def test_post_probe_sends_declared_body_and_reads_declaration(self):
        sent = []

        def response(request):
            sent.append((request.method, json.loads(request.content or b"null")))
            return httpx.Response(402, json=_challenge({"bazaar": POST_EXT}))

        with patch.object(crawlers.public_http, "client", side_effect=lambda **kw:
                          httpx.Client(transport=httpx.MockTransport(response),
                                       follow_redirects=False)):
            result = crawlers.probe_health(ENDPOINT, method_hint="POST",
                                           post_body={"query": "weather in Paris", "limit": 3})
        self.assertEqual(sent, [("POST", {"query": "weather in Paris", "limit": 3})])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["bazaar"]["method"], "POST")

    def test_post_probe_with_body_and_invalid_payment_is_degraded(self):
        def response(_request):
            return httpx.Response(402, json={"error": "pay"})

        with patch.object(crawlers.public_http, "client", side_effect=lambda **kw:
                          httpx.Client(transport=httpx.MockTransport(response),
                                       follow_redirects=False)):
            result = crawlers.probe_health(ENDPOINT, method_hint="POST", post_body={"a": 1})
        self.assertEqual(result["status"], "degraded")


class _DbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "directory.db")
        db.init_db(self.db_path)
        real_connect, real_writer = db.connect, db.writer
        patches = [
            patch.object(db, "connect", side_effect=lambda *_a, **kw: real_connect(
                self.db_path, read_only=kw.get("read_only", False))),
            patch.object(db, "writer", side_effect=lambda *_a, **kw: real_writer(
                self.db_path, immediate=kw.get("immediate", False))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def add(self, slug, url, samples=None):
        with db.writer() as conn:
            _, sid = db.upsert_service(conn, {"slug": slug, "name": slug, "url": url,
                                              "source": "test", "source_id": slug,
                                              "health": "unknown"})
            if samples is not None:
                conn.execute("UPDATE services SET resource_samples=? WHERE id=?",
                             (json.dumps(samples), sid))
        return sid

    def declared(self, sid):
        with db.connect(read_only=True) as conn:
            row = conn.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
        return db.row_to_dict(row).get("declared")


class FeedTests(_DbCase):
    def test_feed_declarations_land_only_on_owning_listings(self):
        other = "https://seller.example.test/api/other"
        sid = self.add("seller", "https://seller.example.test/",
                       samples=[{"url": ENDPOINT}])
        bystander = self.add("bystander", "https://bystander.example.test/api")
        raw = [
            {"resource": ENDPOINT, "extensions": {"bazaar": POST_EXT}},
            {"resource": other, "extensions": {"bazaar": GET_EXT}},
            {"resource": "https://bystander.example.test/api", "extensions": {"bazaar": {"bad": 1}}},
        ]
        stats = crawlers.apply_feed_declarations(raw)
        self.assertEqual(stats, {"declared": 2, "listings": 1, "updated": 1})
        stored = self.declared(sid)
        self.assertEqual(set(stored), {bazaar.key(ENDPOINT)})
        self.assertEqual(stored[bazaar.key(ENDPOINT)]["source"], "cdp-bazaar")
        self.assertIsNone(self.declared(bystander))
        # Re-applying the same feed changes nothing.
        self.assertEqual(crawlers.apply_feed_declarations(raw)["updated"], 0)

    def test_search_filters_on_declared_method(self):
        post = self.add("post-seller", ENDPOINT)
        get = self.add("get-seller", "https://get.example.test/q")
        self.add("plain", "https://plain.example.test/q")
        with db.writer() as conn:
            db.merge_service_declarations(conn, post, {bazaar.key(ENDPOINT): bazaar.extract(POST_EXT)},
                                          "challenge")
            db.merge_service_declarations(conn, get, {bazaar.key("https://get.example.test/q"):
                                                      bazaar.extract(GET_EXT)}, "challenge")
        with db.connect(read_only=True) as conn:
            any_slugs = {r["slug"] for r in db.search(conn, declared=True)}
            post_slugs = {r["slug"] for r in db.search(conn, declared_method="post")}
        self.assertEqual(any_slugs, {"post-seller", "get-seller"})
        self.assertEqual(post_slugs, {"post-seller"})


class HealthJobTests(_DbCase):
    def test_health_uses_declared_post_body_and_get_query(self):
        get_url = "https://get.example.test/q"
        post = self.add("post-seller", ENDPOINT)
        get = self.add("get-seller", get_url)
        plain = self.add("plain", "https://plain.example.test/q")
        with db.writer() as conn:
            db.merge_service_declarations(conn, post, {bazaar.key(ENDPOINT): bazaar.extract(POST_EXT)},
                                          "cdp-bazaar")
            db.merge_service_declarations(conn, get, {bazaar.key(get_url): bazaar.extract(GET_EXT)},
                                          "cdp-bazaar")
        seen = {}
        fresh = dict(bazaar.extract(POST_EXT), body={"query": "fresh"})

        def probe(url, _well_known, inspect_payment=True, method_hint=None,
                  empty_post_probe=False, post_body=None):
            seen[url] = (method_hint, post_body)
            res = {"status": "ok", "latency_ms": 1, "http_status": 402, "x402": True}
            if url == ENDPOINT:
                res["bazaar"] = fresh
            return res

        with (
            patch.object(jobs.crawlers, "probe_health", side_effect=probe),
            patch.object(jobs, "cmd_ownership_recheck"),
            patch.object(jobs, "cmd_rescore"),
            patch.object(jobs, "_maintain_down_since"),
        ):
            self.assertEqual(jobs.cmd_health(only_unknown=True), 0)

        self.assertEqual(seen, {
            ENDPOINT: ("POST", {"query": "weather in Paris", "limit": 3}),
            get_url + "?n=5&q=btc": ("GET", None),  # stored with sorted keys
            "https://plain.example.test/q": (None, None),
        })
        entry = self.declared(post)[bazaar.key(ENDPOINT)]
        self.assertEqual((entry["source"], entry["decl"]["body"]), ("challenge", {"query": "fresh"}))
        self.assertIsNone(self.declared(plain))


if __name__ == "__main__":
    unittest.main()
