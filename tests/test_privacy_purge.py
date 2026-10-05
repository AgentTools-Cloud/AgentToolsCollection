import hashlib
import json
import os
import sqlite3
import asyncio
from contextlib import closing

import pytest

from directory import db


def _text_occurrences(conn, needle):
    matches = []
    for table_row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        table = table_row[0]
        for column in conn.execute("PRAGMA table_info(%s)" % table):
            if "TEXT" not in str(column[2]).upper():
                continue
            count = conn.execute(
                'SELECT COUNT(*) FROM "%s" WHERE instr(COALESCE("%s", \'\'), ?) > 0'
                % (table.replace('"', '""'), column[1].replace('"', '""')),
                (needle,),
            ).fetchone()[0]
            if count:
                matches.append((table, column[1], count))
    return matches


def test_privacy_purge_erases_raw_data_and_blocks_reingestion(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))

    private_host = "private-host-unique77.example.test"
    private_url = "https://%s/paid/check" % private_host
    edited_url = "https://edited-public.example.test/removed"
    private_slug = "private-host-example-test-scan"
    source_id = "private-origin-id"
    similar_host = "private-host-example.test"

    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "123", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(?,?,?,?,?,'verified',1,1)",
            (93, user_id, private_host, "wellknown_file", "token-hash"),
        )
        submission_ids = []
        for suffix in ("check", "score"):
            submission_ids.append(db.create_submission(conn, {
                "name": "Private service " + suffix,
                "url": "https://%s/paid/%s" % (private_host, suffix),
                "description": "See https://third-party.example.test/docs",
                "contact": "owner@example.test",
            }))
        _, listing_id = db.upsert_service(conn, {
            "slug": private_slug,
            "name": "Private service",
            "url": edited_url,
            "source": "submission",
            "source_id": "sub:%d" % submission_ids[0],
            "resource_samples": [{"url": private_url + "/sample"}],
        })
        _, second_listing_id = db.upsert_service(conn, {
            "slug": "second-private-listing",
            "name": "Second private service",
            "url": "https://%s/paid/score" % private_host,
            "source": "x402scan",
            "source_id": source_id,
        })
        _, mcp_id = db.upsert_mcp_server(conn, {
            "slug": "private-mcp",
            "name": "Private MCP",
            "endpoint_url": "https://%s/mcp" % private_host,
            "source": "test",
            "source_id": "private-mcp",
        })
        _, a2a_id = db.upsert_a2a_agent(conn, {
            "slug": "private-a2a",
            "name": "Private A2A",
            "endpoint_url": "https://%s/a2a" % private_host,
            "source": "test",
            "source_id": "private-a2a",
        })
        _, similar_listing_id = db.upsert_service(conn, {
            "slug": "similar-host-service",
            "name": "Similar host service",
            "url": "https://%s/paid/check" % similar_host,
            "source": "test",
            "source_id": "similar-host",
        })
        _, third_party_id = db.upsert_service(conn, {
            "slug": "third-party-service",
            "name": "Third party service",
            "url": "https://third-party.example.test/api",
            "source": "test",
            "source_id": "third-party",
        })
        _, edited_host_peer_id = db.upsert_service(conn, {
            "slug": "edited-host-peer",
            "name": "Edited host peer",
            "url": "https://edited-public.example.test/other",
            "source": "test",
            "source_id": "edited-host-peer",
        })
        _, referenced_only_id = db.upsert_service(conn, {
            "slug": "references-private-host",
            "name": "Reference only",
            "url": "https://reference-only.example.test/api",
            "description": "Legacy docs: https://%s/old" % private_host,
            "source": "test",
            "source_id": "reference-only",
        })
        _, nested_listing_id = db.upsert_service(conn, {
            "slug": "nested-private-resource",
            "name": "Nested private resource",
            "url": "https://public.example.test/listing",
            "resource_samples": [
                {"url": "https://%s/hidden-resource" % private_host},
            ],
            "source": "test",
            "source_id": "nested-private-resource",
        })
        conn.execute(
            "INSERT INTO health_history(service_id,checked_at,status) VALUES(?,1,'ok')",
            (listing_id,),
        )
        conn.execute(
            "INSERT INTO mcp_health_history(server_id,checked_at,status) VALUES(?,1,'ok')",
            (mcp_id,),
        )
        conn.execute(
            "INSERT INTO service_paytos(service_id,chain,address) VALUES(?,'base','0x123')",
            (listing_id,),
        )
        conn.execute(
            "INSERT INTO listing_edits"
            "(kind,listing_id,user_id,ownership_id,field,old_value,new_value,applied_at) "
            "VALUES('x402',?,?,?,?,?,?,1)",
            (second_listing_id, user_id, 93, "url", private_url,
             "https://%s/paid/score" % private_host),
        )
        conn.execute(
            "INSERT INTO endpoint_collisions"
            "(kind,listing_id,occupant_id,user_id,field,old_value,new_value,decision,"
            "reason_code,evidence,uncertainty,created_at) "
            "VALUES('x402',?,?,?,?,?,?,'hold','test','[]','[]',1)",
            (second_listing_id, listing_id, user_id, "url", private_url,
             "https://%s/paid/score" % private_host),
        )
        snapshot = json.dumps({
            "row": {"slug": "old-private", "url": private_url},
        })
        conn.execute(
            "INSERT INTO retired_listings"
            "(kind,slug,original_url,normalized_url,reason,snapshot,status,"
            "retired_at,retired_by) VALUES('x402','old-private',?,?,?,?,'active',1,'test')",
            (private_url, private_url, "test", snapshot),
        )
        retirement_id = conn.execute(
            "SELECT id FROM retired_listings WHERE slug='old-private'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO retired_listing_urls"
            "(retirement_id,normalized_url,original_url) VALUES(?,?,?)",
            (retirement_id, private_url, private_url),
        )
        conn.execute(
            "INSERT INTO listing_retirement_events"
            "(retirement_id,action,actor,note,created_at) "
            "VALUES(?,'retire','test',?,1)",
            (retirement_id, private_url),
        )
        conn.execute(
            "INSERT INTO page_views(ts,kind,slug,ref) VALUES(1,'service',?,?)",
            (private_slug, private_url),
        )
        conn.execute(
            "INSERT INTO page_views(ts,kind,slug) VALUES(2,'service',?)",
            ("sub%d" % submission_ids[0],),
        )
        conn.execute(
            "INSERT INTO page_views(ts,kind,slug) VALUES(3,'service','sub10')"
        )
        similar_claim_host = "private-host-unique770.example.test"
        conn.execute(
            "INSERT INTO domain_ownership"
            "(user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(?,?,?,?, 'verified',1,1)",
            (user_id, similar_claim_host, "wellknown_file", "other-hash"),
        )
        conn.execute(
            "INSERT INTO tool_calls(ts,tool,args,result_slug) VALUES(1,'get',?,?)",
            (json.dumps({"url": private_url}), private_slug),
        )

    with db.writer(str(database)) as conn:
        result = db.privacy_purge_listing(
            conn,
            "x402",
            private_slug,
            actor="test-admin",
            reason="owner_privacy_request",
            submission_ids=submission_ids,
            ownership_ids=[93],
        )

    assert result["listing_id"] == listing_id
    assert result["listings_deleted"] == 4
    assert result["submissions_deleted"] == 2
    assert result["ownerships_deleted"] == 1

    with closing(db.connect(str(database), read_only=True)) as conn:
        assert conn.execute("SELECT 1 FROM services WHERE id=?", (listing_id,)).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM domain_ownership").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM health_history").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mcp_health_history").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM service_paytos").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM page_views").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM endpoint_collisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM retired_listings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM retired_listing_urls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM listing_retirement_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM privacy_suppressions").fetchone()[0] >= 4
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (similar_listing_id,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (third_party_id,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (edited_host_peer_id,)
        ).fetchone() is not None
        referenced_only = conn.execute(
            "SELECT description FROM services WHERE id=?", (referenced_only_id,)
        ).fetchone()
        assert referenced_only is not None
        assert private_host not in referenced_only["description"]
        assert "[removed]" in referenced_only["description"]
        assert conn.execute(
            "SELECT COUNT(*) FROM page_views WHERE slug='sub10'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM domain_ownership WHERE host=?",
            (similar_claim_host,),
        ).fetchone()[0] == 1
        nested_listing = conn.execute(
            "SELECT resource_samples FROM services WHERE id=?",
            (nested_listing_id,),
        ).fetchone()
        assert nested_listing is not None
        assert private_host not in (nested_listing["resource_samples"] or "")
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (second_listing_id,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM mcp_servers WHERE id=?", (mcp_id,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM a2a_agents WHERE id=?", (a2a_id,)
        ).fetchone() is None
        for fts in ("services_fts", "mcp_fts", "a2a_fts"):
            assert conn.execute(
                "SELECT COUNT(*) FROM %s WHERE %s MATCH ?" % (fts, fts),
                ('"unique77"',),
            ).fetchone()[0] == 0
        assert _text_occurrences(conn, private_host) == []
        assert _text_occurrences(conn, private_url) == []

    blocked_rows = (
        {
            "slug": "new-slug",
            "name": "Re-crawled by host",
            "url": "https://%s/new-path" % private_host,
            "source": "other-source",
            "source_id": "other-id",
        },
        {
            "slug": private_slug,
            "name": "Re-crawled by slug",
            "url": "https://public.example.test/new-path",
            "source": "other-source",
            "source_id": "other-id",
        },
        {
            "slug": "another-slug",
            "name": "Re-crawled by source",
            "url": "https://public.example.test/another-path",
            "source": "x402scan",
            "source_id": source_id,
        },
        {
            "slug": "nested-private-url",
            "name": "Re-crawled nested URL",
            "url": "https://public.example.test/listing",
            "resource_samples": [
                {"url": "https://%s/hidden-resource" % private_host},
            ],
            "source": "other-source",
            "source_id": "nested-private-url",
        },
    )
    for row in blocked_rows:
        with pytest.raises(db.PrivacySuppressedListingError):
            with db.writer(str(database)) as conn:
                db.upsert_service(conn, row)

    with pytest.raises(db.PrivacySuppressedListingError):
        with db.writer(str(database)) as conn:
            db.create_submission(conn, {
                "name": "Re-submitted",
                "url": "https://%s/re-submit" % private_host,
            })

    with db.writer(str(database)) as conn:
        created, _ = db.upsert_service(conn, {
            "slug": "unrelated-service",
            "name": "Unrelated",
            "url": "https://public.example.test/unrelated",
            "source": "other-source",
            "source_id": "unrelated-id",
        })
    assert created

    monkeypatch.delenv("PRIVACY_SUPPRESSION_KEY")
    with db.connect(str(database), read_only=True) as conn:
        with pytest.raises(RuntimeError, match="PRIVACY_SUPPRESSION_KEY"):
            db.find_privacy_suppression(conn, "x402", {
                "url": "https://%s/another" % private_host,
            })

    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "wrong-key")
    with db.connect(str(database), read_only=True) as conn:
        with pytest.raises(RuntimeError, match="PRIVACY_SUPPRESSION_KEY"):
            db.find_privacy_suppression(conn, "x402", {
                "url": "https://%s/wrong-key" % private_host,
            })

    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with db.writer(str(database)) as conn:
        public_claim_id = conn.execute(
            "INSERT INTO domain_ownership"
            "(user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(?,?,?,?, 'verified',1,1)",
            (user_id, "public.example.test", "wellknown_file", "public-hash"),
        ).lastrowid
        unrelated_id = conn.execute(
            "SELECT id FROM services WHERE slug='unrelated-service'"
        ).fetchone()[0]
        applied, rejected = db.apply_listing_edits(
            conn, "x402", unrelated_id, user_id, public_claim_id,
            {"description": "See https://%s/owner-edit" % private_host},
        )
    assert applied == []
    assert rejected and "removed" in rejected[0].lower()

    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(database))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from directory import routes

    monkeypatch.setattr(
        routes, "_conn", lambda: db.connect(str(database), read_only=True))
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    for path in (
        "/services/%s" % private_slug,
        "/api/v1/services/%s" % private_slug,
        "/mcp/servers/private-mcp",
        "/api/v1/mcp/servers/private-mcp",
        "/api/v1/mcp/servers/%s" % private_slug,
        "/a2a/agents/private-a2a",
        "/api/v1/a2a/agents/private-a2a",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["x-agent-tools-privacy-suppressed"] == "1"
        assert private_host not in response.text
        assert private_slug not in response.text
    ordinary = client.get("/services/ordinary-missing-listing")
    suppressed = client.get("/services/%s" % private_slug)
    assert ordinary.status_code == suppressed.status_code == 404
    assert ordinary.json() == suppressed.json()


def test_privacy_purge_rejects_scoped_claim_and_holds_write_lock(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))
    host = "scoped-private.example.test"
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "scoped", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,scope_path,method,token_hash,status,created_at,verified_at) "
            "VALUES(?,?,?,?,?,?,'verified',1,1)",
            (101, user_id, host, "/private", "wellknown_file", "hash"),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "scoped-private",
            "name": "Scoped private",
            "url": "https://%s/private" % host,
            "source": "test",
            "source_id": "scoped-private",
        })
    with pytest.raises(ValueError, match="whole-host"):
        with db.writer(str(database)) as conn:
            db.privacy_purge_listing(
                conn, "x402", "scoped-private", "test", ownership_ids=[101])
    with db.connect(str(database), read_only=True) as conn:
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (listing_id,)
        ).fetchone() is not None

    with db.writer(str(database)) as conn:
        other_user_id = db.upsert_user(
            conn, "github", "scoped-other", login="other")
        conn.execute("UPDATE domain_ownership SET user_id=? WHERE id=101",
                     (other_user_id,))
        with pytest.raises(PermissionError, match="ownership changed"):
            db.apply_listing_edits(
                conn, "x402", listing_id, user_id, 101,
                {"description": "stale owner edit"})
        conn.execute("UPDATE domain_ownership SET user_id=? WHERE id=101",
                     (user_id,))

    with db.writer(str(database)) as conn:
        conn.execute("UPDATE domain_ownership SET scope_path=NULL WHERE id=101")
        result = db.privacy_purge_listing(
            conn, "x402", "scoped-private", "test", ownership_ids=[101])
        assert result["listings_deleted"] == 1
        competing = sqlite3.connect(database, timeout=0.05)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute(
                    "INSERT INTO submissions(payload,status,created_at) "
                    "VALUES('{}','pending',1)")
        finally:
            competing.close()


def test_privacy_cli_requires_dry_run_plan(monkeypatch):
    from directory import jobs

    with pytest.raises(ValueError, match="expect-plan-digest"):
        jobs.cmd_privacy_purge(
            "x402", "private", "owner_privacy_request", "test",
            expect_listings=1, expect_submissions=0, expect_ownerships=0,
        )


def test_claim_cannot_reopen_privacy_suppressed_host(tmp_path, monkeypatch):
    from directory import ownership

    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))
    host = "removed-claim.example.test"
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "claim", login="owner")
        db._add_privacy_suppression(
            conn, "*", "host", host, "owner_privacy_request", "test")
    with db.writer(str(database)) as conn:
        with pytest.raises(ownership.ClaimError, match="removed"):
            ownership.issue(conn, user_id, host, "wellknown_file")


def test_purge_rejects_mixed_ids_and_keeps_cross_domain_auxiliary_url(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))
    private_host = "mixed-private.example.test"
    other_host = "unrelated.example.test"
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "mixed", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(401,?,?,?,?,'verified',1,1)",
            (user_id, private_host, "wellknown_file", "hash"),
        )
        first = db.create_submission(conn, {
            "name": "Private", "url": "https://%s/api" % private_host})
        second = db.create_submission(conn, {
            "name": "Other", "url": "https://%s/api" % other_host})
        db.upsert_service(conn, {
            "slug": "mixed-private", "name": "Private",
            "url": "https://%s/api" % private_host,
            "mcp_url": "https://%s/mcp" % other_host,
            "source": "submission", "source_id": "sub:%d" % first,
        })
        _, other_id = db.upsert_service(conn, {
            "slug": "unrelated", "name": "Other",
            "url": "https://%s/api" % other_host,
            "source": "test", "source_id": "unrelated",
        })
    with pytest.raises(ValueError, match="multiple hosts"):
        with db.writer(str(database)) as conn:
            db.privacy_purge_listing(
                conn, "x402", "mixed-private", "test",
                submission_ids=[first, second], ownership_ids=[401])
    with db.writer(str(database)) as conn:
        result = db.privacy_purge_listing(
            conn, "x402", "mixed-private", "test",
            submission_ids=[first], ownership_ids=[401])
        assert result["listings_deleted"] == 1
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (other_id,)
        ).fetchone() is not None


def test_whole_host_purge_refuses_shared_host(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))
    host = "shared.example.test"
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "shared", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(451,?,?,?,?,'verified',1,1)",
            (user_id, host, "wellknown_file", "hash"),
        )
        conn.execute(
            "INSERT INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) "
            "VALUES(?,2,2,'shared',1)", (host,))
        _, first_id = db.upsert_service(conn, {
            "slug": "shared-first", "name": "First",
            "url": "https://%s/first" % host,
            "source": "test", "source_id": "first"})
        _, second_id = db.upsert_service(conn, {
            "slug": "shared-second", "name": "Second",
            "url": "https://%s/second" % host,
            "source": "test", "source_id": "second"})
    with pytest.raises(ValueError, match="shared hosts"):
        with db.writer(str(database)) as conn:
            db.privacy_purge_listing(
                conn, "x402", "shared-first", "test", ownership_ids=[451])
    with closing(db.connect(str(database), read_only=True)) as conn:
        for listing_id in (first_id, second_id):
            assert conn.execute(
                "SELECT 1 FROM services WHERE id=?", (listing_id,)
            ).fetchone() is not None


def test_shared_host_human_verdict_survives_refresh(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "reviewed-shared.example.test"
    with db.writer(str(database)) as conn:
        conn.execute(
            "INSERT INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) "
            "VALUES(?,2,2,'shared',1)", (host,))
        db.refresh_shared_hosts(conn, min_paths=20)
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert db.is_shared_host(conn, host)


def test_idn_url_is_redacted_and_mcp_get_does_not_relog_slug(
        tmp_path, monkeypatch):
    from directory import mcp_app

    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))
    unicode_host = "bücher.example"
    punycode_host = unicode_host.encode("idna").decode("ascii")
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "idn", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(501,?,?,?,?,'verified',1,1)",
            (user_id, punycode_host, "wellknown_file", "hash"),
        )
        db.upsert_service(conn, {
            "slug": "idn-private", "name": "IDN private",
            "url": "https://%s/api" % punycode_host,
            "source": "test", "source_id": "idn-private",
        })
        _, peer_id = db.upsert_service(conn, {
            "slug": "idn-peer", "name": "IDN peer",
            "url": "https://public.example/api",
            "description": "See %s and https://%s/docs" %
                           (unicode_host, unicode_host),
            "source": "test", "source_id": "idn-peer",
        })
        db.privacy_purge_listing(
            conn, "x402", "idn-private", "test", ownership_ids=[501])
    with closing(db.connect(str(database), read_only=True)) as conn:
        description = conn.execute(
            "SELECT description FROM services WHERE id=?", (peer_id,)
        ).fetchone()[0]
        assert unicode_host not in description
        before = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    monkeypatch.setattr(mcp_app, "DB_PATH", str(database))
    result = asyncio.run(mcp_app.get("idn-private"))
    assert result["error"] == "not_found"
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == before


def test_privacy_cli_dry_run_digest_and_apply(tmp_path, monkeypatch, capsys):
    from directory import jobs

    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(database))
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "cli", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(201,?,?,?,?,'verified',1,1)",
            (user_id, "cli-private.example.test", "wellknown_file", "hash"),
        )
        submission_id = db.create_submission(conn, {
            "name": "CLI private",
            "url": "https://cli-private.example.test/api",
        })
        _, listing_id = db.upsert_service(conn, {
            "slug": "cli-private",
            "name": "CLI private",
            "url": "https://cli-private.example.test/api",
            "source": "submission",
            "source_id": "sub:%d" % submission_id,
        })

    jobs.cmd_privacy_purge(
        "x402", None, "owner_privacy_request", "test",
        anchor_submission_id=submission_id,
        ownership_ids=[201], dry_run=True, db_path=str(database))
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["rolled_back"] is True
    assert dry_run["submissions_deleted"] == 1
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (listing_id,)
        ).fetchone() is not None

    with db.writer(str(database)) as conn:
        conn.execute(
            "INSERT INTO page_views(ts,kind,slug,ref) VALUES(1,'service','x',?)",
            ("https://cli-private.example.test/new-reference",))
    monkeypatch.setenv("AGENT_TOOLS_PRIVACY_MAINTENANCE", "1")
    with pytest.raises(RuntimeError, match="plan digest mismatch"):
        jobs.cmd_privacy_purge(
            "x402", None, "owner_privacy_request", "test",
            anchor_submission_id=submission_id, ownership_ids=[201],
            expect_listings=1, expect_submissions=1, expect_ownerships=1,
            expect_plan_digest=dry_run["plan_digest"], db_path=str(database))

    jobs.cmd_privacy_purge(
        "x402", None, "owner_privacy_request", "test",
        anchor_submission_id=submission_id,
        ownership_ids=[201], dry_run=True, db_path=str(database))
    refreshed = json.loads(capsys.readouterr().out)
    jobs.cmd_privacy_purge(
        "x402", None, "owner_privacy_request", "test",
        anchor_submission_id=submission_id, ownership_ids=[201],
        expect_listings=1, expect_submissions=1, expect_ownerships=1,
        expect_plan_digest=refreshed["plan_digest"], db_path=str(database))
    applied = json.loads(capsys.readouterr().out)
    assert applied["rolled_back"] is False
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (listing_id,)
        ).fetchone() is None


def test_privacy_cli_refuses_active_reader_before_delete(
        tmp_path, monkeypatch, capsys):
    from directory import jobs

    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    monkeypatch.setenv("AGENT_TOOLS_PRIVACY_MAINTENANCE", "1")
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "reader", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(301,?,?,?,?,'verified',1,1)",
            (user_id, "reader-private.example.test", "wellknown_file", "hash"),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "reader-private",
            "name": "Reader private",
            "url": "https://reader-private.example.test/api",
            "source": "test",
            "source_id": "reader-private",
        })
    jobs.cmd_privacy_purge(
        "x402", "reader-private", "owner_privacy_request", "test",
        ownership_ids=[301], dry_run=True, db_path=str(database))
    dry_run = json.loads(capsys.readouterr().out)

    reader = sqlite3.connect(database)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM services").fetchall()
    try:
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            jobs.cmd_privacy_purge(
                "x402", "reader-private", "owner_privacy_request", "test",
                ownership_ids=[301], expect_listings=1,
                expect_submissions=0, expect_ownerships=1,
                expect_plan_digest=dry_run["plan_digest"],
                db_path=str(database))
    finally:
        reader.rollback()
        reader.close()
    with db.connect(str(database), read_only=True) as conn:
        assert conn.execute(
            "SELECT 1 FROM services WHERE id=?", (listing_id,)
        ).fetchone() is not None


def test_privacy_finalize_retries_after_vacuum_failure(
        tmp_path, monkeypatch, capsys):
    from directory import jobs

    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    monkeypatch.setenv("AGENT_TOOLS_PRIVACY_MAINTENANCE", "1")
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "finalize", login="owner")
        conn.execute(
            "INSERT INTO domain_ownership"
            "(id,user_id,host,method,token_hash,status,created_at,verified_at) "
            "VALUES(601,?,?,?,?,'verified',1,1)",
            (user_id, "finalize-private.example.test", "wellknown_file", "hash"),
        )
        db.upsert_service(conn, {
            "slug": "finalize-private", "name": "Finalize private",
            "url": "https://finalize-private.example.test/api",
            "source": "test", "source_id": "finalize-private",
        })
    jobs.cmd_privacy_purge(
        "x402", "finalize-private", "owner_privacy_request", "test",
        ownership_ids=[601], dry_run=True, db_path=str(database))
    plan = json.loads(capsys.readouterr().out)

    real_finalize = jobs._finalize_privacy_storage
    monkeypatch.setattr(
        jobs, "_finalize_privacy_storage",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("vacuum failed")))
    with pytest.raises(RuntimeError, match="privacy-finalize"):
        jobs.cmd_privacy_purge(
            "x402", "finalize-private", "owner_privacy_request", "test",
            ownership_ids=[601], expect_listings=1, expect_submissions=0,
            expect_ownerships=1, expect_plan_digest=plan["plan_digest"],
            db_path=str(database))
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert db.get_meta(conn, "privacy_finalize_required") is not None
        assert db.get_by_slug(conn, "finalize-private") is None

    monkeypatch.setattr(jobs, "_finalize_privacy_storage", real_finalize)
    jobs.cmd_privacy_finalize(str(database))
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert db.get_meta(conn, "privacy_finalize_required") is None


def test_owner_locked_submission_cannot_overwrite_listing(
        tmp_path, monkeypatch):
    from directory import jobs

    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, listing_id = db.upsert_service(conn, {
            "slug": "owned-service", "name": "Owner name",
            "url": "https://owned.example.test/api",
            "description": "Owner description", "source": "test",
            "source_id": "owned",})
        conn.execute("UPDATE services SET owner_verified=1 WHERE id=?",
                     (listing_id,))
        submission_id = db.create_submission(conn, {
            "name": "Attacker name", "description": "Attacker description",
            "url": "https://owned.example.test/api",
        })
    real_connect = db.connect
    monkeypatch.setattr(
        db, "connect",
        lambda db_path=db.DEFAULT_DB_PATH, read_only=False, set_wal=True:
            real_connect(str(database), read_only=read_only, set_wal=set_wal))
    monkeypatch.setattr(jobs.crawlers, "fetch_wellknown_resources",
                        lambda *args, **kwargs: {})
    outcome = jobs._approve(submission_id)
    assert outcome["owner_locked"] is True
    with closing(real_connect(str(database), read_only=True)) as conn:
        row = conn.execute(
            "SELECT name,description FROM services WHERE id=?", (listing_id,)
        ).fetchone()
        assert row["name"] == "Owner name"
        assert row["description"] == "Owner description"
        assert conn.execute(
            "SELECT status FROM submissions WHERE id=?", (submission_id,)
        ).fetchone()[0] == "owner_locked"


def test_owner_lock_matches_slug_even_when_endpoint_changes(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, mcp_id = db.upsert_mcp_server(conn, {
            "slug": "owned-mcp", "name": "Owner MCP",
            "endpoint_url": "https://owner.example.test/mcp",
            "source": "test", "source_id": "owned-mcp"})
        conn.execute("UPDATE mcp_servers SET owner_verified=1 WHERE id=?",
                     (mcp_id,))
        locked = db.find_owner_locked_listing(
            conn, "mcp", "https://attacker.example.test/mcp", "owned-mcp")
        assert locked["id"] == mcp_id


def test_pending_claim_cannot_verify_after_host_becomes_shared(
        tmp_path, monkeypatch):
    from directory import ownership

    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "becomes-shared.example.test"
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "pending-shared", login="owner")
        claim_id, token = ownership.issue(
            conn, user_id, host, "wellknown_file")
        conn.execute(
            "INSERT INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) "
            "VALUES(?,20,20,'shared',1)", (host,))
    monkeypatch.setattr(ownership, "probe", lambda *args: (True, "ok"))
    with db.writer(str(database)) as conn:
        ok, detail = ownership.verify_claim(conn, claim_id, token)
        assert ok is False
        assert "shared" in detail
        assert conn.execute(
            "SELECT status FROM domain_ownership WHERE id=?", (claim_id,)
        ).fetchone()[0] == "pending"


def test_finalize_marker_blocks_startup_gate_and_backup(tmp_path):
    from ops import backup_agent_tools as backup

    database = tmp_path / "directory.db"
    target = tmp_path / "snapshot.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        db.set_meta(conn, "privacy_finalize_required", "pending")
    with closing(db.connect(str(database), read_only=True)) as conn:
        with pytest.raises(RuntimeError, match="finalization"):
            db.require_privacy_finalized(conn)
    with pytest.raises(RuntimeError, match="finalization"):
        backup.snapshot(database, target, ("services", "mcp_servers", "a2a_agents", "users"), 10)