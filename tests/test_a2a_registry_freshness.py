import unittest
from unittest.mock import patch

from directory import a2a, crawlers, db


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **_kwargs):
        if url != a2a.A2AREGISTRY_API:
            raise AssertionError(f"unexpected GET {url}")
        return _Response(self._payload)


class A2ARegistryFreshnessTest(unittest.TestCase):
    def _crawl(self, live_card, card_url=None):
        card_url = card_url or "https://agent.example/.well-known/agent.json"
        registry_card = {
            "id": "agent-1",
            "name": "Registry snapshot",
            "description": "stale description",
            "wellKnownURI": card_url,
            "url": "https://agent.example/a2a",
            "skills": [{"id": "old"}],
        }
        payload = {"agents": [registry_card], "total": 1}
        captured = []
        fetch_calls = []

        def fetch(_url, client=None):
            self.assertIsNotNone(client)
            fetch_calls.append(_url)
            if isinstance(live_card, Exception):
                raise live_card
            return live_card

        def upsert(rows):
            captured.extend(rows)
            return len(rows), 0

        with (
            patch.object(a2a.public_http, "client", return_value=_Client(payload)),
            patch.object(a2a, "fetch_agent_card", side_effect=fetch),
            patch.object(a2a, "_upsert_rows", side_effect=upsert),
            patch.object(a2a.db, "with_retry", side_effect=lambda op: op()),
        ):
            result = a2a.crawl_a2aregistry()
        return result, captured, fetch_calls

    def test_live_canonical_card_replaces_registry_snapshot(self):
        live = ({
            "name": "Live canonical agent",
            "description": "fresh description",
            "url": "https://agent.example/a2a",
            "skills": [{"id": "new"}],
        }, "https://agent.example/.well-known/agent-card.json")
        result, rows, fetch_calls = self._crawl(live)
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(rows[0]["name"], "Live canonical agent")
        self.assertEqual(rows[0]["description"], "fresh description")
        self.assertEqual(
            rows[0]["card_url"],
            "https://agent.example/.well-known/agent-card.json",
        )
        self.assertEqual(rows[0]["source_id"], "agent-1")
        self.assertEqual(fetch_calls, ["https://agent.example"])

    def test_registry_snapshot_is_fallback_when_live_fetch_fails(self):
        result, rows, _fetch_calls = self._crawl((None, None))
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(rows[0]["name"], "Registry snapshot")
        self.assertEqual(rows[0]["description"], "stale description")
        self.assertEqual(
            rows[0]["card_url"],
            "https://agent.example/.well-known/agent.json",
        )

    def test_registry_snapshot_is_fallback_when_live_fetch_raises(self):
        result, rows, fetch_calls = self._crawl(RuntimeError("upstream failed"))
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(rows[0]["name"], "Registry snapshot")
        self.assertEqual(fetch_calls, ["https://agent.example"])

    def test_canonical_registry_card_is_refreshed_at_its_url(self):
        canonical = "https://agent.example/.well-known/agent-card.json"
        live = ({
            "name": "Fresh canonical agent",
            "description": "fresh canonical description",
            "url": "https://agent.example/a2a",
            "skills": [{"id": "new"}],
        }, canonical)
        result, rows, fetch_calls = self._crawl(live, card_url=canonical)
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(rows[0]["name"], "Fresh canonical agent")
        self.assertEqual(rows[0]["card_url"], canonical)
        self.assertEqual(fetch_calls, [canonical])

    def test_canonical_registry_snapshot_is_fallback_when_live_fetch_fails(self):
        canonical = "https://agent.example/.well-known/agent-card.json"
        result, rows, fetch_calls = self._crawl(
            RuntimeError("upstream failed"), card_url=canonical,
        )
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(rows[0]["name"], "Registry snapshot")
        self.assertEqual(rows[0]["card_url"], canonical)
        self.assertEqual(fetch_calls, [canonical])

    def test_snapshot_fallback_does_not_overwrite_existing_live_row(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "directory.db")
            db.init_db(database)
            with db.writer(database) as conn:
                _, agent_id = db.upsert_a2a_agent(conn, {
                    "slug": "live-agent", "name": "Live canonical agent",
                    "description": "fresh description",
                    "endpoint_url": "https://agent.example/a2a",
                    "card_url": "https://agent.example/.well-known/agent-card.json",
                    "skills": [{"id": "new"}],
                    "source": "a2aregistry", "source_id": "agent-1",
                })
                fallback = a2a.card_to_row({
                    "name": "Registry snapshot",
                    "description": "stale description",
                    "url": "https://agent.example/a2a",
                    "skills": [{"id": "old"}],
                }, "https://agent.example/.well-known/agent.json",
                    source="a2aregistry", source_id="agent-1")
                fallback["_snapshot_fallback"] = True
                created, got_id = db.upsert_a2a_agent(conn, fallback)
                self.assertFalse(created)
                self.assertEqual(got_id, agent_id)
            with db.connect(database, read_only=True) as conn:
                row = conn.execute(
                    "SELECT name,description,card_url,skills FROM a2a_agents "
                    "WHERE id=?", (agent_id,),
                ).fetchone()
                self.assertEqual(row["name"], "Live canonical agent")
                self.assertEqual(row["description"], "fresh description")
                self.assertTrue(row["card_url"].endswith("agent-card.json"))
                self.assertIn("new", row["skills"])

    def test_mid_pagination_failure_is_partial(self):
        first = _Response({"agents": [{
            "id": "agent-1", "name": "One",
            "wellKnownURI": "https://agent.example/.well-known/agent-card.json",
            "url": "https://agent.example/a2a",
        }], "total": 2})

        class Client:
            calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, url, **_kwargs):
                self.calls += 1
                if url == a2a.A2AREGISTRY_API and self.calls == 1:
                    return first
                raise a2a.httpx.ReadTimeout("page failed")

        captured = []
        with (
            patch.object(a2a.public_http, "client", return_value=Client()),
            patch.object(a2a, "fetch_agent_card", return_value=(None, None)),
            patch.object(a2a, "_upsert_rows", side_effect=lambda rows: (
                captured.extend(rows) or len(rows), 0
            )),
            patch.object(a2a.db, "with_retry", side_effect=lambda op: op()),
        ):
            with self.assertRaises(crawlers.PartialCrawl) as raised:
                a2a.crawl_a2aregistry(page_size=1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(raised.exception.items["resolved"], 1)

    def test_empty_page_before_total_is_partial(self):
        payloads = iter([
            {"agents": [{
                "id": "agent-1", "name": "One",
                "wellKnownURI": "https://agent.example/.well-known/agent-card.json",
                "url": "https://agent.example/a2a",
            }], "total": 2},
            {"agents": [], "total": 2},
        ])

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _url, **_kwargs):
                return _Response(next(payloads))

        with (
            patch.object(a2a.public_http, "client", return_value=Client()),
            patch.object(a2a, "fetch_agent_card", return_value=(None, None)),
            patch.object(a2a, "_upsert_rows", return_value=(1, 0)),
            patch.object(a2a.db, "with_retry", side_effect=lambda op: op()),
        ):
            with self.assertRaisesRegex(crawlers.PartialCrawl, "empty page"):
                a2a.crawl_a2aregistry(page_size=1)

    def test_max_pages_before_total_is_partial(self):
        payload = {"agents": [{
            "id": "agent-1", "name": "One",
            "wellKnownURI": "https://agent.example/.well-known/agent-card.json",
            "url": "https://agent.example/a2a",
        }], "total": 3}
        with (
            patch.object(
                a2a.public_http, "client", return_value=_Client(payload),
            ),
            patch.object(a2a, "fetch_agent_card", return_value=(None, None)),
            patch.object(a2a, "_upsert_rows", return_value=(1, 0)),
            patch.object(a2a.db, "with_retry", side_effect=lambda op: op()),
        ):
            with self.assertRaisesRegex(crawlers.PartialCrawl, "max_pages"):
                a2a.crawl_a2aregistry(page_size=1, max_pages=1)

    def test_missing_total_full_page_continues_pagination(self):
        payloads = iter([
            {"agents": [{
                "id": "agent-1", "name": "One",
                "wellKnownURI": "https://one.example/.well-known/agent-card.json",
                "url": "https://one.example/a2a",
            }]},
            {"agents": [{
                "id": "agent-2", "name": "Two",
                "wellKnownURI": "https://two.example/.well-known/agent-card.json",
                "url": "https://two.example/a2a",
            }]},
            {"agents": []},
        ])

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _url, **_kwargs):
                return _Response(next(payloads))

        captured = []
        with (
            patch.object(a2a.public_http, "client", return_value=Client()),
            patch.object(a2a, "fetch_agent_card", return_value=(None, None)),
            patch.object(a2a, "_upsert_rows", side_effect=lambda rows: (
                captured.extend(rows) or len(rows), 0
            )),
            patch.object(a2a.db, "with_retry", side_effect=lambda op: op()),
        ):
            result = a2a.crawl_a2aregistry(page_size=1)
        self.assertEqual(result["resolved"], 2)
        self.assertEqual(len(captured), 2)


if __name__ == "__main__":
    unittest.main()