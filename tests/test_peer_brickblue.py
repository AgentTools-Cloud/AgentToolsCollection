import json
import unittest
from unittest.mock import patch

import httpx

from directory import crawlers, peers

FEED = peers.BRICKBLUE_FEED


def _page(records, nxt=None):
    lines = [json.dumps({"hub": "brick.blue", "version": "0.1.0", "what": "a\u2028b"})]
    lines += [json.dumps(r) for r in records]
    lines.append(json.dumps({"done": nxt is None, "count": len(records), "next": nxt}))
    return "\n".join(lines) + "\n"


def _mcp(rid, url, availability="live", transport="streamable-http", name=None):
    return {"id": rid, "kind": "mcp", "endpoint": url, "origin": "https://x-demo.io",
            "transport": transport, "availability": availability, "name": name,
            "uptime": 0.99, "latencyMs": 12, "access": "open"}


class BrickBlueParserTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(crawlers, "url_retired", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_page_skips_header_and_follows_only_same_feed(self):
        recs, nxt = peers.brickblue_page(_page([_mcp("a1", "https://a-demo.io/mcp")],
                                                FEED + "?afterId=a1"))
        self.assertEqual([r["id"] for r in recs], ["a1"])
        self.assertEqual(nxt, FEED + "?afterId=a1")
        self.assertIsNone(peers.brickblue_page(_page([], "https://evil-demo.io/api/v1/agents.ndjson"))[1])
        self.assertIsNone(peers.brickblue_page(_page([]))[1])

    def test_line_separator_inside_a_record_does_not_split_it(self):
        rec = _mcp("a1", "https://a-demo.io/mcp", name="one\u2028two")
        recs, _ = peers.brickblue_page(_page([rec]))
        self.assertEqual(recs[0]["name"], "one\u2028two")

    def test_split_keeps_live_https_unkeyed_and_drops_measurements(self):
        recs = [
            _mcp("a1", "https://a-demo.io/mcp"),
            _mcp("a2", "https://b-demo.io/sse", transport="http-sse"),
            _mcp("a3", "https://c-demo.io/mcp", availability="retired"),
            _mcp("a4", "http://d-demo.io/mcp"),
            _mcp("a5", "https://e-demo.io/mcp?api_key=secret"),
            {"id": "g1", "kind": "a2a", "origin": "https://agent-demo.io", "availability": "live",
             "endpoint": "https://agent-demo.io/a2a"},
            {"id": "g2", "kind": "a2a", "origin": "https://down-demo.io", "availability": "down"},
        ]
        mcp, leads = peers.brickblue_split(recs)
        self.assertEqual([(m["endpoint_url"], m["transport"]) for m in mcp],
                         [("https://a-demo.io/mcp", "streamable-http"),
                          ("https://b-demo.io/sse", "sse")])
        self.assertEqual(leads, [{"card_url": "https://agent-demo.io", "source_id": "g1",
                                  "source_url": "https://brick.blue/agent/g1"}])
        self.assertNotIn("uptime", mcp[0])

    def test_row_uses_server_info_and_links_the_record(self):
        cand = {"endpoint_url": "https://a-demo.io/mcp", "transport": "streamable-http",
                "source_id": "a1", "name": "From feed"}
        row = peers.brickblue_mcp_row(cand, {"auth": False, "info": {"name": "Weather", "description": "Forecasts"}})
        self.assertEqual((row["name"], row["description"], row["source"]), ("Weather", "Forecasts", "brick-blue"))
        self.assertEqual(row["source_url"], "https://brick.blue/agent/a1")
        self.assertIsNone(row["auth_method"])
        row = peers.brickblue_mcp_row(cand, {"auth": True, "info": {}})
        self.assertEqual((row["name"], row["auth_method"]), ("From feed", "oauth"))


def _client_for(handler):
    return lambda **kw: httpx.Client(transport=httpx.MockTransport(handler),
                                     follow_redirects=kw.get("follow_redirects", False))


class BrickBlueVerifyTests(unittest.TestCase):
    def verify(self, handler, transport="streamable-http"):
        with patch("directory.public_http.client", side_effect=_client_for(handler)):
            return peers.brickblue_verify("https://a-demo.io/mcp", transport)

    def test_anonymous_initialize(self):
        def h(req):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {"serverInfo": {"name": "Weather"}}})
        self.assertEqual(self.verify(h), {"auth": False, "info": {"name": "Weather"}})

    def test_oauth_challenge_counts_and_plain_401_does_not(self):
        bearer = lambda req: httpx.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="x"'})
        self.assertEqual(self.verify(bearer), {"auth": True, "info": {}})
        self.assertIsNone(self.verify(lambda req: httpx.Response(401)))
        self.assertIsNone(self.verify(lambda req: httpx.Response(200, text="<html>")))

    def test_sse_endpoint_event(self):
        ok = lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                        content=b"event: endpoint\ndata: /messages?s=1\n\n")
        self.assertEqual(self.verify(ok, "sse"), {"auth": False, "info": {}})
        page = lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=b"hi")
        self.assertIsNone(self.verify(page, "sse"))


class BrickBlueFetchTests(unittest.TestCase):
    def test_new_endpoints_only_and_mark_pending(self):
        recs = [_mcp("a1", "https://known-demo.io/mcp"), _mcp("a2", "https://new-demo.io/mcp"),
                _mcp("a3", "https://silent-demo.io/mcp")]
        answers = {"https://new-demo.io/mcp": {"auth": False, "info": {"name": "New"}}}

        class Conn:
            def execute(self, *_):
                return [("https://KNOWN-demo.io/mcp/",)]

            def close(self):
                pass

        with patch.object(peers, "_due", return_value=True), \
                patch.object(peers, "_brickblue_records", return_value=recs), \
                patch.object(peers.db, "connect", return_value=Conn()), \
                patch.object(peers, "_owner_removed_hosts", return_value=set()), \
                patch.object(crawlers, "url_retired", return_value=False), \
                patch.object(peers, "brickblue_verify", side_effect=lambda u, t: answers.get(u)):
            rows = peers.fetch_brickblue_mcp()
        self.assertEqual([r["endpoint_url"] for r in rows], ["https://new-demo.io/mcp"])
        self.assertEqual(peers._PENDING.pop("brick-blue"), ("brick-blue-mcp", None))

    def test_records_are_slimmed_to_pointers(self):
        rec = dict(_mcp("a1", "https://a-demo.io/mcp"), skills=["x"] * 50, description="long")
        recs, _ = peers.brickblue_page(_page([rec]))
        self.assertEqual(set(recs[0]), set(peers.BRICKBLUE_FIELDS))

    def test_streamed_lines_split_on_newline_only(self):
        body = _page([_mcp("a1", "https://a-demo.io/mcp", name="x\u2028y")], FEED + "?afterId=a1")

        class Resp:
            def iter_text(self):
                for i in range(0, len(body), 7):
                    yield body[i:i + 7]

        recs, nxt = peers.brickblue_page(peers._ndjson_lines(Resp()))
        self.assertEqual((recs[0]["name"], nxt), ("x\u2028y", FEED + "?afterId=a1"))

    def test_registered_as_peer_source(self):
        self.assertIs(peers.MCP_FETCHERS["brick-blue"], peers.fetch_brickblue_mcp)
        self.assertIs(peers.A2A_LEAD_SOURCES["brick-blue"], peers.brickblue_fetch_leads)
        self.assertEqual(peers.SOURCE_URLS["brick-blue"], "https://brick.blue")


if __name__ == "__main__":
    unittest.main()
