from contextlib import closing, contextmanager
import asyncio
import json
import sqlite3
import threading
import time
from unittest.mock import Mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import db, limits, routes


@contextmanager
def _route_database(monkeypatch, path):
    db.init_db(str(path))
    real_connect = db.connect
    real_writer = db.writer
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(path))
    monkeypatch.setattr(
        routes, "_conn", lambda: real_connect(str(path), read_only=True)
    )
    monkeypatch.setattr(
        routes.db, "connect",
        lambda *_args, **kwargs: real_connect(
            str(path), read_only=kwargs.get("read_only", False),
            set_wal=kwargs.get("set_wal", True), timeout=kwargs.get("timeout", 30.0)
        ),
    )
    monkeypatch.setattr(
        routes.db, "writer",
        lambda *_args, **kwargs: real_writer(
            str(path), immediate=kwargs.get("immediate", False),
            timeout=kwargs.get("timeout", 0.05)
        ),
    )
    yield real_connect


def _client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_mcp_submission_completes_source_and_payment_classification(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    with _route_database(monkeypatch, database) as real_connect:
        monkeypatch.setattr(routes, "_require_public_url", _noop_public_url)
        monkeypatch.setattr(routes, "_enforce_submit_limit", lambda *_args: None)
        monkeypatch.setattr(
            routes.directory_crawlers, "url_retired", lambda _url: False
        )
        monkeypatch.setattr(
            routes.directory_crawlers, "probe_mcp_health",
            lambda _url: {"status": "ok", "latency_ms": 5, "http_status": 200},
        )
        monkeypatch.setattr(
            routes.directory_crawlers, "verify_x402",
            lambda _url: {"status": "uncertain", "verified_url": None,
                          "payment": None},
        )
        monkeypatch.setattr(
            routes.directory_mailer, "send_admin_notification",
            lambda *_args, **_kwargs: None,
        )

        response = _client().post("/api/v1/mcp/submit", json={
            "url": "https://submit.example.test/mcp",
            "contact": "operator@example.test",
        })

    assert response.status_code == 200, response.text
    assert response.json()["x402"] is False
    with real_connect(str(database), read_only=True) as conn:
        row = conn.execute(
            "SELECT endpoint_url,x402_supported FROM mcp_servers"
        ).fetchone()
        assert row["endpoint_url"] == "https://submit.example.test/mcp"
        assert row["x402_supported"] == 0
        assert conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0


def test_mcp_submission_rolls_back_source_when_paid_target_is_owner_locked(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    paid_url = "https://owned.example.test/pay"
    with _route_database(monkeypatch, database) as real_connect:
        with routes.db.writer(immediate=True) as conn:
            _, service_id = db.upsert_service(conn, {
                "slug": "owned-paid", "name": "Owned paid",
                "url": paid_url, "source": "owner", "source_id": "owned",
            })
            conn.execute(
                "UPDATE services SET owner_verified=1 WHERE id=?", (service_id,)
            )
        monkeypatch.setattr(routes, "_require_public_url", _noop_public_url)
        monkeypatch.setattr(routes, "_enforce_submit_limit", lambda *_args: None)
        monkeypatch.setattr(
            routes.directory_crawlers, "url_retired", lambda _url: False
        )
        monkeypatch.setattr(
            routes.directory_crawlers, "probe_mcp_health",
            lambda _url: {"status": "ok", "latency_ms": 5, "http_status": 200},
        )
        monkeypatch.setattr(
            routes.directory_crawlers, "verify_x402",
            lambda _url: {
                "status": "verified", "verified_url": paid_url,
                "payment": {
                    "scheme": "exact", "network": "eip155:8453",
                    "networks": ["eip155:8453"], "amount_raw": "1000000",
                    "max_amount_usdc": 1.0,
                    "asset": "0x" + "1" * 40,
                    "pay_to": "0x" + "2" * 40,
                },
            },
        )

        response = _client().post("/api/v1/mcp/submit", json={
            "url": "https://submit.example.test/mcp",
            "contact": "operator@example.test",
        })

        assert response.status_code == 409, response.text
        with real_connect(str(database), read_only=True) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM mcp_servers"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM services"
            ).fetchone()[0] == 1


async def _noop_public_url(_url):
    return None


def _mock_mcp_submission(monkeypatch):
    monkeypatch.setattr(routes, "_require_public_url", _noop_public_url)
    monkeypatch.setattr(routes, "SUBMIT_RATE_LIMIT_PER_DAY", 10)
    monkeypatch.setattr(routes.directory_crawlers, "url_retired", lambda _url: False)
    monkeypatch.setattr(
        routes.directory_crawlers, "probe_mcp_health",
        lambda _url: {"status": "ok", "latency_ms": 5, "http_status": 200},
    )
    monkeypatch.setattr(
        routes.directory_crawlers, "verify_x402",
        lambda _url: {"status": "uncertain", "verified_url": None, "payment": None},
    )
    notification = Mock(return_value=None)
    monkeypatch.setattr(routes.directory_mailer, "send_admin_notification", notification)
    return notification


def test_mcp_submission_main_lock_is_retryable_and_atomic(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database) as real_connect:
        notification = _mock_mcp_submission(monkeypatch)
        limits.activate_rate_limit_store(str(database))
        client = _client()
        payload = {
            "url": "https://submit.example.test/mcp",
            "contact": "operator@example.test",
        }
        with closing(real_connect(str(database))) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            response = client.post("/api/v1/mcp/submit", json=payload)
            elapsed = time.monotonic() - started
            assert response.status_code == 503
            assert response.headers["retry-after"] == "2"
            assert elapsed < 3.0
            notification.assert_not_called()
            blocker.rollback()

        with closing(real_connect(str(database), read_only=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 0

        response = client.post("/api/v1/mcp/submit", json=payload)
        assert response.status_code == 200, response.text
        with closing(real_connect(str(database), read_only=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 1
        notification.assert_called_once()


def test_rest_submission_main_lock_is_retryable(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database) as real_connect:
        _mock_mcp_submission(monkeypatch)
        review = Mock(return_value={"status": "pending"})
        monkeypatch.setattr(routes.directory_jobs, "review_submission", review)
        limits.activate_rate_limit_store(str(database))
        client = _client()
        payload = {
            "name": "Fixture service", "url": "https://submit.example.test/pay",
            "contact": "operator@example.test",
        }
        with closing(real_connect(str(database))) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            response = client.post("/api/v1/submit", json=payload)
            assert response.status_code == 503
            assert response.headers["retry-after"] == "2"
            assert time.monotonic() - started < 3.0
            review.assert_not_called()
            blocker.rollback()
        with closing(real_connect(str(database), read_only=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
        response = client.post("/api/v1/submit", json=payload)
        assert response.status_code == 200, response.text
        repeated = client.post("/api/v1/submit", json=payload)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["status"] == "already_pending"
        assert repeated.json()["submission_id"] == response.json()["submission_id"]
        review.assert_called_once()


def test_a2a_submission_main_lock_is_retryable(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database) as real_connect:
        notification = _mock_mcp_submission(monkeypatch)
        endpoint = "https://submit.example.test/a2a"
        card_url = "https://submit.example.test/.well-known/agent-card.json"
        monkeypatch.setattr(
            routes.directory_a2a, "fetch_agent_card", lambda *_args: ({"name": "Fixture"}, card_url))
        monkeypatch.setattr(routes.directory_a2a, "card_to_row", lambda *_args, **_kwargs: {
            "slug": "fixture-agent", "name": "Fixture", "endpoint_url": endpoint,
            "card_url": card_url, "source": "submission", "source_id": card_url,
        })
        limits.activate_rate_limit_store(str(database))
        client = _client()
        payload = {"url": endpoint, "contact": "operator@example.test"}
        with closing(real_connect(str(database))) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            response = client.post("/api/v1/a2a/submit", json=payload)
            assert response.status_code == 503
            assert response.headers["retry-after"] == "2"
            assert time.monotonic() - started < 3.0
            notification.assert_not_called()
            blocker.rollback()
        with closing(real_connect(str(database), read_only=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM a2a_agents").fetchone()[0] == 0
        response = client.post("/api/v1/a2a/submit", json=payload)
        assert response.status_code == 200, response.text
        with closing(real_connect(str(database), read_only=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM a2a_agents").fetchone()[0] == 1
        notification.assert_called_once()


def test_mcp_submission_recovers_from_brief_write_lock(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database) as real_connect:
        notification = _mock_mcp_submission(monkeypatch)
        limits.activate_rate_limit_store(str(database))
        with closing(real_connect(str(database))) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            release = threading.Timer(0.15, blocker.rollback)
            release.start()
            try:
                response = _client().post("/api/v1/mcp/submit", json={
                    "url": "https://submit.example.test/mcp",
                    "contact": "operator@example.test",
                })
            finally:
                release.join(timeout=2.0)
            assert response.status_code == 200, response.text
        notification.assert_called_once()


def test_mcp_submission_uses_the_short_retry_budget(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database) as real_connect:
        notification = _mock_mcp_submission(monkeypatch)
        limits.activate_rate_limit_store(str(database))
        with closing(real_connect(str(database))) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            release = threading.Timer(1.2, blocker.rollback)
            release.start()
            started = time.monotonic()
            try:
                response = _client().post("/api/v1/mcp/submit", json={
                    "url": "https://submit.example.test/mcp",
                    "contact": "operator@example.test",
                })
                elapsed = time.monotonic() - started
            finally:
                release.join(timeout=2.0)
            assert response.status_code == 200, response.text
            assert elapsed < 3.0
        notification.assert_called_once()


def test_submission_write_does_not_block_http_event_loop(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database):
        _mock_mcp_submission(monkeypatch)
        limits.activate_rate_limit_store(str(database))
        original = routes._submission_write
        entered = threading.Event()
        released = threading.Event()

        def delayed_write(operation):
            entered.set()
            released.wait(1.5)
            return original(operation)

        monkeypatch.setattr(routes, "_submission_write", delayed_write)

        async def exercise():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=_client().app),
                    base_url="https://testserver") as client:
                submit = asyncio.create_task(client.post("/api/v1/mcp/submit", json={
                    "url": "https://submit.example.test/mcp",
                    "contact": "operator@example.test",
                }))
                try:
                    assert await asyncio.to_thread(entered.wait, 1.0)
                    result = await client.get("/api/v1/stats")
                    assert result.status_code == 200
                    assert not submit.done()
                finally:
                    released.set()
                result = await submit
                assert result.status_code == 200, result.text

        asyncio.run(exercise())


def test_parallel_mcp_submissions_keep_one_listing(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database) as real_connect:
        _mock_mcp_submission(monkeypatch)
        limits.activate_rate_limit_store(str(database))

        async def exercise():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=_client().app),
                    base_url="https://testserver") as client:
                payload = {"url": "https://submit.example.test/mcp",
                           "contact": "operator@example.test"}
                responses = await asyncio.gather(*[
                    client.post("/api/v1/mcp/submit", json=payload) for _ in range(2)])
                assert all(response.status_code == 200 for response in responses)
                assert len({response.json()["slug"] for response in responses}) == 1

        asyncio.run(exercise())
        with closing(real_connect(str(database), read_only=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 1


def test_mcp_submission_limit_lock_is_retryable(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database) as real_connect:
        monkeypatch.setattr(routes, "_require_public_url", _noop_public_url)
        monkeypatch.setattr(
            routes.directory_crawlers, "url_retired", lambda _url: False
        )

        def blocked_counter(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def unexpected(*_args, **_kwargs):
            raise AssertionError("busy limiter reached probing or persistence")

        monkeypatch.setattr(db, "hit_rate_limit", blocked_counter)
        monkeypatch.setattr(routes.directory_crawlers, "probe_mcp_health", unexpected)
        monkeypatch.setattr(routes.directory_mailer, "send_admin_notification", unexpected)
        response = _client().post("/api/v1/mcp/submit", json={
            "url": "https://submit.example.test/mcp",
            "contact": "operator@example.test",
        })

        assert response.status_code == 503
        assert response.headers["retry-after"] == "2"
        with real_connect(str(database), read_only=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 0


def test_suppressed_submissions_stop_before_network_or_owner_lookup(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with _route_database(monkeypatch, database):
        with db.writer(str(database)) as conn:
            db._add_privacy_suppression(
                conn, "*", "host", "removed.example.test",
                "owner_privacy_request", "test",
            )

        def unexpected(*_args, **_kwargs):
            raise AssertionError("suppressed submission reached network or lookup")

        monkeypatch.setattr(routes, "_require_public_url", unexpected)
        monkeypatch.setattr(routes.db, "find_service_by_url", unexpected)
        monkeypatch.setattr(routes.db, "find_mcp_by_endpoint", unexpected)
        monkeypatch.setattr(routes.db, "find_a2a_by_card_url", unexpected)
        client = _client()
        cases = (
            ("/api/v1/submit", {
                "name": "Removed", "url": "https://removed.example.test/pay",
                "contact": "operator@example.test",
            }),
            ("/api/v1/mcp/submit", {
                "url": "https://removed.example.test/mcp",
                "contact": "operator@example.test",
            }),
            ("/api/v1/a2a/submit", {
                "url": "https://removed.example.test/a2a",
                "contact": "operator@example.test",
            }),
        )
        responses = [client.post(path, json=payload) for path, payload in cases]

    for response in responses:
        assert response.status_code == 404
        assert response.headers["x-agent-tools-privacy-suppressed"] == "1"
        assert response.headers["cache-control"] == "no-store"
        assert "removed.example.test" not in response.text


def test_pending_submission_origin_has_hostname_boundary(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        conn.execute(
            "INSERT INTO submissions(payload,status,created_at) "
            "VALUES(?,'pending',1)",
            (json.dumps({"url": "https://example.com.evil/pay"}),),
        )
        assert db.find_pending_submission(
            conn, "https://example.com/api"
        ) is None
        conn.execute(
            "INSERT INTO submissions(payload,status,created_at) "
            "VALUES(?,'pending',2)",
            (json.dumps({"url": "https://EXAMPLE.com:443/first"}),),
        )
        pending = db.find_pending_submission(
            conn, "https://example.com/second"
        )
        assert pending is not None
        assert pending["created_at"] == 2