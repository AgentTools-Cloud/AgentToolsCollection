"""x402 rows mirrored from an MCP server or A2A agent get the mcp/a2a tag and the channel
category only when the paid resource is that endpoint itself (2026-11-21).

Before, every mirror was tagged and filed by the channel we found it through, so a site's
paid HTTP API that its MCP server or agent card pointed to read as an MCP server or an A2A
agent.
"""
import os
import sys
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from directory import db, reverify_x402  # noqa: E402
from test_resources_search_and_payto_limit import _TempDB  # noqa: E402

PAY = {"network": "eip155:8453", "max_amount_usdc": 0.01}
ENDPOINTS = {"mcp": "https://seller.example/mcp", "a2a": "https://seller.example/a2a"}
OTHER_PAID = "https://seller.example/api/v1/search"
CHANNEL = {"mcp": ("mcp", "model-context-protocol-mcp"), "a2a": ("a2a", "a2a-agent")}


def _verdict(url):
    return {"status": "verified", "verified_url": url, "payment": PAY}


def _task(delivery):
    return {"slug": "seller", "name": "Seller", "endpoint": ENDPOINTS[delivery],
            "delivery": delivery, "source": "registry", "source_id": "seller"}


class BuildService(unittest.TestCase):
    def test_paid_endpoint_keeps_channel_labels(self):
        for delivery, (tag, category) in CHANNEL.items():
            s = reverify_x402._build_service(_task(delivery), _verdict(ENDPOINTS[delivery]))
            self.assertEqual(s["tags"], [tag, "x402"], delivery)
            self.assertEqual(s["category"], category, delivery)

    def test_other_paid_path_is_not_labelled_by_channel(self):
        for delivery in CHANNEL:
            s = reverify_x402._build_service(_task(delivery), _verdict(OTHER_PAID))
            self.assertEqual(s["tags"], ["x402"], delivery)
            self.assertEqual(s["category"], "general", delivery)
            self.assertEqual(s["url"], OTHER_PAID)
        mcp = reverify_x402._build_service(_task("mcp"), _verdict(OTHER_PAID))
        self.assertEqual(mcp["mcp_url"], ENDPOINTS["mcp"])


class VerifyAndMirror(unittest.TestCase):
    def test_labels_follow_the_paid_resource(self):
        captured = []

        def mirror(service, protect_owner=None):
            captured.append(service)
            return {"created": True, "slug": service["slug"]}

        with patch.object(reverify_x402, "_mirror_service", side_effect=mirror):
            for url in (OTHER_PAID, ENDPOINTS["mcp"]):
                reverify_x402.verify_and_mirror(ENDPOINTS["mcp"], slug="seller",
                                                delivery="mcp", verdict=_verdict(url))
        self.assertEqual([s["tags"] for s in captured], [["x402"], ["mcp", "x402"]])
        self.assertEqual([s["category"] for s in captured],
                         ["general", "model-context-protocol-mcp"])


class NightlyGather(_TempDB):
    def test_gathered_tasks_do_not_preset_the_channel_category(self):
        with db.writer() as c:
            db.upsert_mcp_server(c, {"slug": "seller-mcp", "name": "Seller MCP",
                                     "endpoint_url": ENDPOINTS["mcp"], "health": "ok",
                                     "source": "mcp-registry", "source_id": "seller"})
        with closing(db.connect(read_only=True)) as c:
            tasks = reverify_x402._gather(c, ("mcp",), None, False)
        self.assertEqual(len(tasks), 1)
        s = reverify_x402._build_service(tasks[0], _verdict(OTHER_PAID))
        self.assertEqual((s["tags"], s["category"]), (["x402"], "general"))


if __name__ == "__main__":
    unittest.main()
