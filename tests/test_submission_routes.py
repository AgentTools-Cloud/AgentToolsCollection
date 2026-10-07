from contextlib import contextmanager
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import db, routes


@contextmanager
def _route_database(monkeypatch, path):
    db.init_db(str(path))
    real_connect = db.connect
    real_writer = db.writer
    monkeypatch.setattr(
        routes, "_conn", lambda: real_connect(str(path), read_only=True)
    )
    monkeypatch.setattr(
        routes.db, "connect",
        lambda *_args, **kwargs: real_connect(
            str(path), read_only=kwargs.get("read_only", False)
        ),
    )
    monkeypatch.setattr(
        routes.db, "writer",
        lambda *_args, **kwargs: real_writer(
            str(path), immediate=kwargs.get("immediate", False)
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