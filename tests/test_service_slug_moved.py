"""Approval links and merged duplicates must not 404 (2026-11-02)."""
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import db, routes


def _setup(tmp_path, monkeypatch):
    path = str(tmp_path / "d.db")
    db.init_db(path)
    real_connect = db.connect
    monkeypatch.setattr(routes, "_conn",
                        lambda: real_connect(path, read_only=True))
    c = real_connect(path)
    now = int(time.time())
    c.execute("INSERT INTO services(slug,name,url,source,source_id,health,"
              "created_at,updated_at) VALUES('host-scan','Host',"
              "'https://host.example/v1/a','x402scan','abc','ok',?,?)",
              (now, now))
    sid = c.execute("SELECT id FROM services WHERE slug='host-scan'").fetchone()[0]
    for source, source_id in (("submission", "sub:77"), ("x402", "x402:host-bazaar")):
        c.execute("INSERT INTO listing_sources(kind,listing_id,source,source_id,"
                  "first_seen,last_seen) VALUES('x402',?,?,?,?,?)",
                  (sid, source, source_id, now, now))
    c.commit()
    app = FastAPI()
    app.include_router(routes.router)
    return real_connect(path), TestClient(app)


def test_submission_slug_folded_into_existing_row_redirects(tmp_path, monkeypatch):
    conn, client = _setup(tmp_path, monkeypatch)
    assert db.resolve_moved_service_slug(conn, "host-example-sub77") == "host-scan"
    r = client.get("/services/host-example-sub77", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/services/host-scan"


def test_merged_duplicate_slug_redirects(tmp_path, monkeypatch):
    conn, client = _setup(tmp_path, monkeypatch)
    r = client.get("/services/host-bazaar", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/services/host-scan"


def test_unknown_slug_still_404(tmp_path, monkeypatch):
    conn, client = _setup(tmp_path, monkeypatch)
    assert db.resolve_moved_service_slug(conn, "nobody-sub999") is None
    assert client.get("/services/nobody-sub999",
                      follow_redirects=False).status_code == 404
