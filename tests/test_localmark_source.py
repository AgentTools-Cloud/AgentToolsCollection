import unittest
from unittest.mock import patch

from directory import a2a


def _rec(aid, card_url, endpoint=None, passed=True):
    return {"agent_id": aid, "card_url": card_url,
            "endpoint_url": endpoint or card_url.rsplit("/.well-known", 1)[0] + "/a2a",
            "profile_url": f"https://localmark.ai/agents/{aid}",
            "observations": {"latest_card_check": {"passed": passed}}}


class LocalMarkLeadsTest(unittest.TestCase):
    def test_filters_known_failed_http_and_duplicates(self):
        recs = [
            _rec(1, "https://a.example/.well-known/agent-card.json"),
            _rec(2, "https://b.example/.well-known/agent-card.json"),       # known card
            _rec(3, "https://c.example/.well-known/agent-card.json",
                 endpoint="https://c.example/rpc"),                         # known endpoint
            _rec(4, "https://d.example/.well-known/agent-card.json", passed=False),
            _rec(5, "http://e.example/.well-known/agent-card.json"),
            _rec(6, "https://a.example/.well-known/agent-card.json/"),      # dup of 1
            {"agent_id": 7},
        ]
        known = {"https://b.example/.well-known/agent-card.json",
                 "https://c.example/rpc"}
        leads = a2a.localmark_leads(recs, known)
        self.assertEqual([l["source_id"] for l in leads], ["1"])
        self.assertEqual(leads[0]["source_url"], "https://localmark.ai/agents/1")

    def test_per_host_and_total_caps(self):
        recs = [_rec(i, f"https://bulk.example/agents/{i}/.well-known/agent-card.json")
                for i in range(50)]
        recs += [_rec(100 + i, f"https://h{i}.example/.well-known/agent-card.json")
                 for i in range(10)]
        leads = a2a.localmark_leads(recs, set(), per_host=20, max_new=25)
        hosts = [l["card_url"].split("/")[2] for l in leads]
        self.assertEqual(hosts.count("bulk.example"), 20)
        self.assertEqual(len(leads), 25)


class LocalMarkCrawlTest(unittest.TestCase):
    def test_rows_fetched_from_publisher_with_source_link(self):
        recs = [_rec(9, "https://agent.example/.well-known/agent-card.json")]
        card = {"name": "Agent X", "url": "https://agent.example/a2a",
                "skills": [{"id": "s"}]}
        written = []

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def ingest(_conn, kind, source, batch, _upsert):
            self.assertEqual((kind, source), ("a2a", "localmark"))
            written.extend(batch)
            return {"added": len(batch), "updated": 0, "rejected": 0}

        class _Writer:
            def __enter__(self):
                return object()

            def __exit__(self, *_a):
                return False

        with (
            patch.object(a2a, "_fetch_localmark_feed", return_value=recs),
            patch.object(a2a, "_known_a2a_urls", return_value=set()),
            patch.object(a2a.public_http, "client", return_value=_Client()),
            patch.object(a2a, "fetch_agent_card",
                         return_value=(card, recs[0]["card_url"])) as fetch,
            patch("directory.ingestion.ingest_batch", side_effect=ingest),
            patch.object(a2a.db, "writer", return_value=_Writer()),
            patch.object(a2a.db, "with_retry", side_effect=lambda op: op()),
        ):
            res = a2a.crawl_localmark()
        self.assertEqual(fetch.call_args.kwargs["homepage"], False)
        self.assertEqual(res["inserted"], 1)
        row = written[0]
        self.assertEqual(row["source"], "localmark")
        self.assertEqual(row["source_id"], "9")
        self.assertEqual(row["source_url"], "https://localmark.ai/agents/9")
        self.assertEqual(row["slug"], "agent-x-agent-example")
        self.assertEqual(row["name"], "Agent X")


if __name__ == "__main__":
    unittest.main()
