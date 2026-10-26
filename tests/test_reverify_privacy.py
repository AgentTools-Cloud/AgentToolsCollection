from contextlib import closing
import json

import pytest

from directory import db
from directory import reverify_x402


def test_reverify_metadata_respects_privacy_suppression(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))
    private_host = "removed-reverify.example.test"
    with db.writer(str(database)) as conn:
        db._add_privacy_suppression(
            conn, "*", "host", private_host, "owner_privacy_request", "test")
        _, service_id = db.upsert_service(conn, {
            "slug": "public-service", "name": "Public",
            "url": "https://public.example.test/api",
            "source": "test", "source_id": "public-service",
        })
        assert reverify_x402._privacy_suppressed_metadata(
            conn,
            resource_samples=json.dumps([
                {"url": "https://%s/private" % private_host},
            ]),
        )
        assert reverify_x402._privacy_suppressed_metadata(
            conn,
            payment=json.dumps({"payTo": "https://%s/pay" % private_host}),
        )
        conn.execute(
            "UPDATE services SET resource_samples=? WHERE id=? AND ?=0",
            (json.dumps([{"url": "https://%s/private" % private_host}]),
             service_id,
             int(reverify_x402._privacy_suppressed_metadata(
                 conn, resource_samples=json.dumps([
                     {"url": "https://%s/private" % private_host},
                 ]))),
            ),
        )
    with closing(db.connect(str(database), read_only=True)) as conn:
        assert conn.execute(
            "SELECT resource_samples FROM services WHERE id=?", (service_id,)
        ).fetchone()[0] is None


def test_build_service_uses_verified_paid_resource():
    task = {
        "slug": "property-check",
        "name": "Property Check",
        "description": "Property intelligence",
        "endpoint": "https://agents.example.test/a2a",
        "homepage": "https://agents.example.test/",
        "delivery": "a2a",
        "source": "registry",
        "source_id": "property-check",
        "category": "a2a-agent",
    }
    verdict = {
        "status": "verified",
        "verified_url": "https://agents.example.test/v1/property",
        "payment": {
            "network": "eip155:8453",
            "networks": ["eip155:8453", "eip155:137", "solana:mainnet"],
            "max_amount_usdc": 1.0,
        },
    }
    service = reverify_x402._build_service(task, verdict)

    assert service["url"] == "https://agents.example.test/v1/property"
    assert service["mcp_url"] is None
    assert service["chains"] == ["base", "polygon", "solana"]
    task["verdict"] = verdict
    assert not reverify_x402._endpoint_is_x402(task)

    task["verdict"] = {
        **verdict,
        "verified_url": "https://agents.example.test/a2a",
    }
    assert reverify_x402._endpoint_is_x402(task)


def test_marker_only_verdict_is_not_a_paid_resource():
    task = {
        "endpoint": "https://agents.example.test/a2a",
        "verdict": {
            "status": "verified",
            "verified_url": None,
            "payment": {"network": "eip155:8453"},
        },
    }
    assert not reverify_x402._endpoint_is_x402(task)


def test_endpoint_identity_preserves_scheme_port_path_case_and_query():
    endpoint = "https://agents.example.test:8443/A2A?tenant=a"
    for other in (
        "http://agents.example.test:8443/A2A?tenant=a",
        "https://agents.example.test/A2A?tenant=a",
        "https://agents.example.test:8443/a2a?tenant=a",
        "https://agents.example.test:8443/A2A?tenant=b",
    ):
        task = {
            "endpoint": endpoint,
            "verdict": {"status": "verified", "verified_url": other},
        }
        assert not reverify_x402._endpoint_is_x402(task)


def test_mirror_cannot_overwrite_owner_verified_target(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, listing_id = db.upsert_service(conn, {
            "slug": "owned-paid", "name": "Owner name",
            "url": "https://owned.example.test/pay",
            "description": "Owner description",
            "source": "owner", "source_id": "owned-paid",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
    real_connect = db.connect
    real_writer = db.writer
    monkeypatch.setattr(
        reverify_x402.db, "connect",
        lambda *_args, **kwargs: real_connect(
            str(database), read_only=kwargs.get("read_only", False)
        ),
    )
    monkeypatch.setattr(
        reverify_x402.db, "writer",
        lambda *_args, **kwargs: real_writer(
            str(database), immediate=kwargs.get("immediate", False)
        ),
    )

    result = reverify_x402.verify_and_mirror(
        "https://attacker.example.test/a2a",
        slug="attacker", name="Attacker name",
        description="Attacker description", delivery="a2a",
        source="attacker", source_id="attacker",
        verdict={
            "status": "verified",
            "verified_url": "https://owned.example.test/pay",
            "payment": {
                "network": "eip155:8453",
                "networks": ["eip155:8453"],
                "max_amount_usdc": 1.0,
            },
        },
    )

    assert result["owner_locked"] is True
    with closing(real_connect(str(database), read_only=True)) as conn:
        row = conn.execute(
            "SELECT name,description,category FROM services WHERE id=?",
            (listing_id,),
        ).fetchone()
        assert row["name"] == "Owner name"
        assert row["description"] == "Owner description"


def test_owner_lock_uses_endpoint_not_slug(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        fixtures = (
            ("x402", db.upsert_service, {
                "slug": "same-name", "name": "Owned x402",
                "url": "https://owned.example.test/pay",
                "source": "owner", "source_id": "x402",
            }, "https://new.example.test/pay"),
            ("mcp", db.upsert_mcp_server, {
                "slug": "same-name-mcp", "name": "Owned MCP",
                "endpoint_url": "https://owned.example.test/mcp",
                "source": "owner", "source_id": "mcp",
            }, "https://new.example.test/mcp"),
            ("a2a", db.upsert_a2a_agent, {
                "slug": "same-name-a2a", "name": "Owned A2A",
                "endpoint_url": "https://owned.example.test/a2a",
                "card_url": "https://owned.example.test/card.json",
                "source": "owner", "source_id": "a2a",
            }, "https://new.example.test/a2a"),
        )
        for kind, upsert, row, other_endpoint in fixtures:
            _, listing_id = upsert(conn, row)
            conn.execute(
                "UPDATE %s SET owner_verified=1 WHERE id=?"
                % db._KIND_TABLE[kind], (listing_id,),
            )
            primary = row[db._PRIMARY_ENDPOINT[kind]]
            assert db.find_owner_locked_listing(
                conn, kind, primary, row["slug"]
            )["id"] == listing_id
            assert db.find_owner_locked_listing(
                conn, kind, other_endpoint, row["slug"]
            ) is None


def test_cross_source_mirror_only_adds_provenance(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, listing_id = db.upsert_service(conn, {
            "slug": "existing-paid", "name": "Existing name",
            "url": "https://existing.example.test/pay?tenant=A",
            "description": "Existing description",
            "source": "first", "source_id": "existing-paid",
        })
    real_connect = db.connect
    real_writer = db.writer
    monkeypatch.setattr(
        reverify_x402.db, "connect",
        lambda *_args, **kwargs: real_connect(
            str(database), read_only=kwargs.get("read_only", False)
        ),
    )
    monkeypatch.setattr(
        reverify_x402.db, "writer",
        lambda *_args, **kwargs: real_writer(
            str(database), immediate=kwargs.get("immediate", False)
        ),
    )
    result = reverify_x402.verify_and_mirror(
        "https://protocol.example.test/a2a",
        slug="incoming", name="Incoming name",
        description="Incoming description", delivery="a2a",
        source="registry", source_id="incoming",
        verdict={
            "status": "verified",
            "verified_url": "https://existing.example.test/pay?tenant=A",
            "payment": {
                "scheme": "exact", "network": "eip155:8453",
                "networks": ["eip155:8453"], "amount_raw": "1000000",
                "max_amount_usdc": 1.0, "pay_to": "0x123",
            },
        },
    )
    assert result["duplicate"] is True
    with closing(real_connect(str(database), read_only=True)) as conn:
        row = conn.execute(
            "SELECT name,description FROM services WHERE id=?", (listing_id,)
        ).fetchone()
        assert row["name"] == "Existing name"
        assert row["description"] == "Existing description"
        assert conn.execute(
            "SELECT COUNT(*) FROM listing_sources "
            "WHERE kind='x402' AND listing_id=? AND source='a2a'",
            (listing_id,),
        ).fetchone()[0] == 1


def test_owner_canonical_seller_absorbs_declared_mcp_paid_resource(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "seller.example.test"
    paid = f"https://{host}/compatibility-receipt"
    mcp = f"https://{host}/mcp"
    pay_to = "0x" + "1" * 40
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}",
            "resource_count": 1,
            "resource_samples": [{"url": paid, "method": "GET"}],
            "payment": {"network": "eip155:8453", "pay_to": pay_to},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )

        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "mcp-mirror", "name": "MCP mirror",
            "url": paid, "mcp_url": mcp,
            "payment": {"network": "base", "pay_to": pay_to},
            "source": "mcp", "source_id": "registry:server-1",
        })

        assert result["duplicate"] is True
        assert result["listing_id"] == listing_id
        assert result["canonical_seller"] is True
        rows = conn.execute(
            "SELECT id,url,mcp_url,name FROM services ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["url"] == f"https://{host}"
        assert rows[0]["mcp_url"] == mcp
        assert rows[0]["name"] == "Canonical seller"


def test_same_host_mirror_is_not_folded_without_manifest_membership(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "seller.example.test"
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        pay_to = "0x" + "1" * 40
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}",
            "resource_samples": [{"url": f"https://{host}/known"}],
            "payment": {"network": "base", "pay_to": pay_to},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )

        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "independent-resource", "name": "Independent resource",
            "url": f"https://{host}/different",
            "mcp_url": f"https://{host}/mcp",
            "payment": {"network": "eip155:8453", "pay_to": pay_to},
            "source": "mcp", "source_id": "registry:server-2",
        })

        assert result["created"] is True
        assert conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 2


@pytest.mark.parametrize("canonical_payto,incoming_payto", [
    (None, "0x" + "1" * 40),
    ("0x" + "1" * 40, None),
    ("0x" + "1" * 40, "0x" + "2" * 40),
])
def test_declared_resource_is_not_folded_without_matching_payto(
        tmp_path, canonical_payto, incoming_payto):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "seller.example.test"
    paid = f"https://{host}/declared"
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}",
            "resource_samples": [{"url": paid}],
            "payment": ({"network": "base", "pay_to": canonical_payto}
                        if canonical_payto else None),
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )

        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "independent-resource", "name": "Independent resource",
            "url": paid, "mcp_url": f"https://{host}/mcp",
            "payment": ({"network": "eip155:8453", "pay_to": incoming_payto}
                        if incoming_payto else None),
            "source": "mcp", "source_id": "registry:server-3",
        })

        assert result["created"] is True
        assert conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 2


def test_historical_payto_does_not_authorize_seller_fold(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "seller.example.test"
    paid = f"https://{host}/declared"
    old_payto = "0x" + "1" * 40
    current_payto = "0x" + "2" * 40
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}",
            "resource_samples": [{"url": paid}],
            "payment": {"network": "base", "pay_to": current_payto},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
        conn.execute(
            "INSERT INTO service_paytos(service_id,chain,address) "
            "VALUES(?,'base',?)", (listing_id, old_payto),
        )

        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "old-wallet-resource", "name": "Old wallet resource",
            "url": paid, "mcp_url": f"https://{host}/mcp",
            "payment": {"network": "eip155:8453", "pay_to": old_payto},
            "source": "mcp", "source_id": "registry:old-wallet",
        })

        assert result["created"] is True
        assert conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 2


def test_same_address_on_different_chain_does_not_fold(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "seller.example.test"
    paid = f"https://{host}/declared"
    pay_to = "0x" + "1" * 40
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}", "resource_samples": [{"url": paid}],
            "payment": {"network": "eip155:8453", "pay_to": pay_to},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "other-chain", "name": "Other chain", "url": paid,
            "payment": {"network": "eip155:1", "pay_to": pay_to},
            "source": "a2a", "source_id": "registry:other-chain",
        })
        assert result["created"] is True


def test_shared_host_never_folds_declared_resource(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "shared.example.test"
    paid = f"https://{host}/declared"
    pay_to = "0x" + "1" * 40
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}", "resource_samples": [{"url": paid}],
            "payment": {"network": "base", "pay_to": pay_to},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
        conn.execute(
            "INSERT INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) "
            "VALUES(?,20,20,'shared',1)", (host,),
        )
        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "tenant-resource", "name": "Tenant resource", "url": paid,
            "payment": {"network": "eip155:8453", "pay_to": pay_to},
            "source": "mcp", "source_id": "registry:tenant",
        })
        assert result["created"] is True


def test_multiple_canonical_candidates_prevent_fold(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "seller.example.test"
    paid = f"https://{host}/declared"
    pay_to = "0x" + "1" * 40
    payment = json.dumps({"network": "base", "pay_to": pay_to})
    samples = json.dumps([{"url": paid}])
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        for slug, url in (("root-one", f"https://{host}"),
                          ("root-two", f"https://{host}/")):
            conn.execute(
                "INSERT INTO services(slug,name,url,source,source_id,payment,"
                "resource_samples,owner_verified,created_at,updated_at) "
                "VALUES(?,?,?,'directory',?,?,?,1,1,1)",
                (slug, slug, url, slug, payment, samples),
            )
        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "mirror", "name": "Mirror", "url": paid,
            "payment": {"network": "eip155:8453", "pay_to": pay_to},
            "source": "mcp", "source_id": "registry:ambiguous",
        })
        assert result["created"] is True
        assert conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 3


def test_base58_payto_comparison_preserves_case():
    upper = reverify_x402._current_payment_identities({
        "payment": {"network": "solana", "pay_to": "AbCdEf123"},
    })
    lower = reverify_x402._current_payment_identities({
        "payment": {"network": "solana", "pay_to": "abcdef123"},
    })
    assert upper.isdisjoint(lower)


def test_conflicting_secondary_chain_prevents_fold(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "seller.example.test"
    paid = f"https://{host}/declared"
    base_payto = "0x" + "1" * 40
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}", "resource_samples": [{"url": paid}],
            "payment": {"accepts": [
                {"network": "base", "payTo": base_payto},
                {"network": "polygon", "payTo": "0x" + "2" * 40},
            ]},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
        conn.execute(
            "INSERT INTO shared_hosts(host,path_count,listing_count,verdict,updated_at) "
            "VALUES(?,1,1,'single_operator',1)", (host,),
        )
        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "conflict", "name": "Conflict", "url": paid,
            "payment": {"accepts": [
                {"network": "eip155:8453", "payTo": base_payto},
                {"network": "eip155:137", "payTo": "0x" + "3" * 40},
            ]},
            "source": "mcp", "source_id": "registry:conflict",
        })
        assert result["created"] is True


def test_unreviewed_two_path_host_does_not_fold(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "gateway.example.test"
    paid = f"https://{host}/tenant-a/pay"
    pay_to = "0x" + "1" * 40
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}", "resource_samples": [{"url": paid}],
            "payment": {"network": "base", "pay_to": pay_to},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
        db.upsert_mcp_server(conn, {
            "slug": "tenant-b", "name": "Tenant B",
            "endpoint_url": f"https://{host}/tenant-b/mcp",
            "source": "test", "source_id": "tenant-b",
        })
        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "tenant-a", "name": "Tenant A", "url": paid,
            "payment": {"network": "eip155:8453", "pay_to": pay_to},
            "source": "mcp", "source_id": "registry:tenant-a",
        })
        assert result["created"] is True


def test_unreviewed_query_tenants_do_not_fold(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "gateway.example.test"
    paid = f"https://{host}/?tenant=a"
    pay_to = "0x" + "1" * 40
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "api_key", "seller-owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,),
        )
        _, listing_id = db.upsert_service(conn, {
            "slug": "canonical-seller", "name": "Canonical seller",
            "url": f"https://{host}", "resource_samples": [{"url": paid}],
            "payment": {"network": "base", "pay_to": pay_to},
            "source": "directory", "source_id": "canonical-seller",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
        db.upsert_mcp_server(conn, {
            "slug": "tenant-b", "name": "Tenant B",
            "endpoint_url": f"https://{host}/?tenant=b",
            "source": "test", "source_id": "tenant-b",
        })
        result = reverify_x402._mirror_service_in_tx(conn, {
            "slug": "tenant-a", "name": "Tenant A", "url": paid,
            "payment": {"network": "eip155:8453", "pay_to": pay_to},
            "source": "mcp", "source_id": "registry:tenant-a",
        })
        assert result["created"] is True


def test_paid_primary_url_does_not_dedup_against_mcp_alias(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    paid = "https://agents.example.test/v1/property"
    with db.writer(str(database)) as conn:
        _, alias_id = db.upsert_service(conn, {
            "slug": "protocol-alias", "name": "Protocol alias",
            "url": "https://agents.example.test/other",
            "mcp_url": paid,
            "source": "first", "source_id": "protocol-alias",
        })
    real_connect = db.connect
    real_writer = db.writer
    monkeypatch.setattr(
        reverify_x402.db, "connect",
        lambda *_args, **kwargs: real_connect(
            str(database), read_only=kwargs.get("read_only", False)
        ),
    )
    monkeypatch.setattr(
        reverify_x402.db, "writer",
        lambda *_args, **kwargs: real_writer(
            str(database), immediate=kwargs.get("immediate", False)
        ),
    )

    result = reverify_x402.verify_and_mirror(
        "https://agents.example.test/a2a",
        slug="property-check", name="Property Check", delivery="a2a",
        source="registry", source_id="property-check",
        verdict={
            "status": "verified", "verified_url": paid,
            "payment": {
                "scheme": "exact", "network": "eip155:8453",
                "networks": ["eip155:8453"], "amount_raw": "1000000",
                "max_amount_usdc": 1.0,
                "pay_to": "0x" + "1" * 40,
            },
        },
    )

    assert result["created"] is True
    with closing(real_connect(str(database), read_only=True)) as conn:
        rows = conn.execute(
            "SELECT id,url,mcp_url FROM services ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] == alias_id
        assert rows[1]["url"] == paid


def test_database_endpoint_identity_preserves_path_case_and_query(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, first_id = db.upsert_service(conn, {
            "slug": "tenant-upper", "name": "Upper",
            "url": "https://example.test/API?tenant=A",
            "source": "test", "source_id": "upper",
        })
        _, second_id = db.upsert_service(conn, {
            "slug": "tenant-lower", "name": "Lower",
            "url": "https://example.test/api?tenant=a",
            "source": "test", "source_id": "lower",
        })
        assert first_id != second_id
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (first_id,)
        )
        assert db.find_owner_locked_listing(
            conn, "x402", "https://example.test/api?tenant=a"
        ) is None
        assert db.find_owner_locked_listing(
            conn, "x402", "https://example.test/API?tenant=A"
        )["id"] == first_id


def test_stable_source_cannot_move_onto_another_service(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, source_id = db.upsert_service(conn, {
            "slug": "source-service", "name": "Source",
            "url": "https://example.test/source",
            "source": "registry", "source_id": "stable",
        })
        _, occupant_id = db.upsert_service(conn, {
            "slug": "occupant-service", "name": "Occupant",
            "url": "https://example.test/paid",
            "source": "other", "source_id": "occupant",
        })
        created, result_id = db.upsert_service(conn, {
            "slug": "source-service", "name": "Attacker update",
            "url": "https://example.test/paid",
            "source": "registry", "source_id": "stable",
        })
        assert not created and result_id == source_id
        rows = conn.execute(
            "SELECT id,name,url FROM services WHERE id IN (?,?) ORDER BY id",
            (source_id, occupant_id),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["url"] != rows[1]["url"]
        assert conn.execute(
            "SELECT name FROM services WHERE id=?", (occupant_id,)
        ).fetchone()[0] == "Occupant"


def test_secondary_source_identity_cannot_move_to_new_endpoint(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, listing_id = db.upsert_mcp_server(conn, {
            "slug": "first", "name": "First",
            "endpoint_url": "https://example.test/first",
            "source": "first", "source_id": "first",
        })
        created, same_id = db.upsert_mcp_server(conn, {
            "slug": "secondary", "name": "Secondary",
            "endpoint_url": "https://example.test/first",
            "source": "secondary", "source_id": "stable",
        })
        assert not created and same_id == listing_id

        created, result_id = db.upsert_mcp_server(conn, {
            "slug": "moved", "name": "Moved",
            "endpoint_url": "https://example.test/new",
            "source": "secondary", "source_id": "stable",
        })
        assert not created and result_id == listing_id
        rows = conn.execute(
            "SELECT id,name,endpoint_url FROM mcp_servers ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "First"
        assert rows[0]["endpoint_url"] == "https://example.test/first"


def test_a2a_endpoint_and_card_cannot_join_two_rows(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, first_id = db.upsert_a2a_agent(conn, {
            "slug": "first", "name": "First",
            "endpoint_url": "https://example.test/a2a/first",
            "card_url": "https://example.test/cards/first.json",
            "source": "first", "source_id": "first",
        })
        _, second_id = db.upsert_a2a_agent(conn, {
            "slug": "second", "name": "Second",
            "endpoint_url": "https://example.test/a2a/second",
            "card_url": "https://example.test/cards/second.json",
            "source": "second", "source_id": "second",
        })

        created, result_id = db.upsert_a2a_agent(conn, {
            "slug": "joined", "name": "Joined",
            "endpoint_url": "https://example.test/a2a/first",
            "card_url": "https://example.test/cards/second.json",
            "source": "third", "source_id": "joined",
        })
        assert not created and result_id in (first_id, second_id)
        rows = conn.execute(
            "SELECT id,name,endpoint_url,card_url FROM a2a_agents ORDER BY id"
        ).fetchall()
        assert [(row["name"], row["endpoint_url"], row["card_url"])
                for row in rows] == [
            ("First", "https://example.test/a2a/first",
             "https://example.test/cards/first.json"),
            ("Second", "https://example.test/a2a/second",
             "https://example.test/cards/second.json"),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM listing_sources "
            "WHERE kind='a2a' AND source='third' AND source_id='joined'"
        ).fetchone()[0] == 0


def test_owner_verified_metadata_is_not_overwritten_by_recrawl(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, listing_id = db.upsert_service(conn, {
            "slug": "owned", "name": "Owner name",
            "description": "Owner description",
            "url": "https://owned.example.test/pay",
            "source": "registry", "source_id": "stable",
        })
        conn.execute(
            "UPDATE services SET owner_verified=1 WHERE id=?", (listing_id,)
        )
        db.upsert_service(conn, {
            "slug": "owned", "name": "Crawler name",
            "description": "Crawler description",
            "url": "https://owned.example.test/pay",
            "source": "registry", "source_id": "stable",
        })
        row = conn.execute(
            "SELECT name,description FROM services WHERE id=?", (listing_id,)
        ).fetchone()
        assert row["name"] == "Owner name"
        assert row["description"] == "Owner description"


def test_metadata_recrawl_preserves_authoritative_x402_flags(tmp_path):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, mcp_id = db.upsert_mcp_server(conn, {
            "slug": "mcp", "name": "MCP",
            "endpoint_url": "https://example.test/mcp",
            "source": "test", "source_id": "mcp",
        })
        _, a2a_id = db.upsert_a2a_agent(conn, {
            "slug": "a2a", "name": "A2A",
            "endpoint_url": "https://example.test/a2a",
            "source": "test", "source_id": "a2a",
        })
        conn.execute(
            "UPDATE mcp_servers SET x402_supported=0 WHERE id=?", (mcp_id,)
        )
        conn.execute(
            "UPDATE a2a_agents SET x402_supported=0 WHERE id=?", (a2a_id,)
        )
        db.upsert_mcp_server(conn, {
            "slug": "mcp", "name": "MCP refreshed",
            "endpoint_url": "https://example.test/mcp",
            "x402_supported": True,
            "source": "test", "source_id": "mcp",
        })
        db.upsert_a2a_agent(conn, {
            "slug": "a2a", "name": "A2A refreshed",
            "endpoint_url": "https://example.test/a2a",
            "x402_supported": True,
            "source": "test", "source_id": "a2a",
        })
        assert conn.execute(
            "SELECT x402_supported FROM mcp_servers WHERE id=?", (mcp_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT x402_supported FROM a2a_agents WHERE id=?", (a2a_id,)
        ).fetchone()[0] == 0


def test_endpoint_edit_clears_endpoint_derived_payment_state(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "edit-reset.example.test"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(conn, "github", "edit-reset", login="owner")
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,)
        )
        _, service_id = db.upsert_service(conn, {
            "slug": "edit-reset", "name": "Edit reset",
            "url": f"https://{host}/old", "source": "test",
            "source_id": "edit-reset", "chains": ["base"],
            "well_known_url": f"https://{host}/.well-known/x402",
            "price_min": 1.0, "price_max": 1.0,
            "payment": {"network": "base", "pay_to": "0x123"},
            "x402_ok": 1,
        })
        conn.execute(
            "INSERT INTO service_paytos(service_id,chain,address) "
            "VALUES(?, 'base', '0x123')", (service_id,)
        )
        applied, rejected = db.apply_listing_edits(
            conn, "x402", service_id, user_id, ownership_id,
            {"url": f"https://{host}/new"},
        )
        assert applied == ["url"] and not rejected
        row = conn.execute(
            "SELECT chains,price_min,price_max,payment,x402_ok,well_known_url "
            "FROM services WHERE id=?", (service_id,)
        ).fetchone()
        assert all(value is None for value in row)
        assert conn.execute(
            "SELECT COUNT(*) FROM service_paytos WHERE service_id=?",
            (service_id,),
        ).fetchone()[0] == 0


def test_owner_endpoint_edits_reject_occupied_urls(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    host = "edit-collision.example.test"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    with db.writer(str(database)) as conn:
        user_id = db.upsert_user(
            conn, "github", "edit-collision", login="owner"
        )
        ownership_id, _token = __import__(
            "directory.ownership", fromlist=["issue"]
        ).issue(conn, user_id, host, "wellknown_file")
        conn.execute(
            "UPDATE domain_ownership SET status='verified',verified_at=1 "
            "WHERE id=?", (ownership_id,)
        )
        fixtures = {
            "x402": (db.upsert_service, "url", {
                "source": "test", "category": "general",
            }),
            "mcp": (db.upsert_mcp_server, "endpoint_url", {
                "source": "test",
            }),
            "a2a": (db.upsert_a2a_agent, "endpoint_url", {
                "source": "test",
                "card_url": f"https://{host}/cards/source.json",
            }),
        }
        for kind, (upsert, endpoint_field, extra) in fixtures.items():
            first = {
                "slug": f"{kind}-source", "name": f"{kind} source",
                endpoint_field: f"https://{host}/{kind}/source",
                "source_id": f"{kind}-source", **extra,
            }
            second = {
                "slug": f"{kind}-target", "name": f"{kind} target",
                endpoint_field: f"https://{host}/{kind}/target",
                "source_id": f"{kind}-target", **extra,
            }
            if kind == "a2a":
                second["card_url"] = f"https://{host}/cards/target.json"
            _, first_id = upsert(conn, first)
            _, second_id = upsert(conn, second)

            applied, rejected = db.apply_listing_edits(
                conn, kind, first_id, user_id, ownership_id,
                {endpoint_field: second[endpoint_field]},
            )
            assert not applied
            assert rejected and "already used" in rejected[0]
            table = db._KIND_TABLE[kind]
            rows = conn.execute(
                f"SELECT id,{endpoint_field} FROM {table} "
                "WHERE id IN (?,?) ORDER BY id", (first_id, second_id)
            ).fetchall()
            assert len(rows) == 2
            assert rows[0][endpoint_field] != rows[1][endpoint_field]


def test_reverify_discards_results_after_endpoint_changes(
        tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    db.init_db(str(database))
    old_mcp = "https://example.test/mcp/old"
    new_mcp = "https://example.test/mcp/new"
    old_paid = "https://example.test/pay/old"
    new_paid = "https://example.test/pay/new"
    with db.writer(str(database)) as conn:
        _, mcp_id = db.upsert_mcp_server(conn, {
            "slug": "cas-mcp", "name": "CAS MCP",
            "endpoint_url": old_mcp,
            "source": "test", "source_id": "cas-mcp",
        })
        _, service_id = db.upsert_service(conn, {
            "slug": "cas-paid", "name": "CAS paid", "url": old_paid,
            "source": "test", "source_id": "cas-paid", "health": "ok",
        })
    real_connect = db.connect
    real_writer = db.writer
    monkeypatch.setattr(
        reverify_x402.db, "connect",
        lambda *_args, **kwargs: real_connect(
            str(database), read_only=kwargs.get("read_only", False)
        ),
    )
    monkeypatch.setattr(
        reverify_x402.db, "writer",
        lambda *_args, **kwargs: real_writer(
            str(database), immediate=kwargs.get("immediate", False)
        ),
    )

    changed = set()

    def probe(task):
        table = task["table"]
        with real_writer(str(database), immediate=True) as conn:
            if table == "mcp_servers" and table not in changed:
                conn.execute(
                    "UPDATE mcp_servers SET endpoint_url=? WHERE id=?",
                    (new_mcp, mcp_id),
                )
                changed.add(table)
            elif table == "services" and table not in changed:
                conn.execute(
                    "UPDATE services SET url=? WHERE id=?",
                    (new_paid, service_id),
                )
                changed.add(table)
        task["verdict"] = {
            "status": "verified",
            "verified_url": task["endpoint"],
            "payment": {
                "scheme": "exact", "network": "eip155:8453",
                "networks": ["eip155:8453"], "amount_raw": "1000000",
                "max_amount_usdc": 1.0,
                "pay_to": "0x" + "1" * 40,
            },
        }
        if table == "services":
            task["resources"] = {
                "resource_count": 7,
                "resource_samples": [{"url": old_paid}],
            }
        return task

    monkeypatch.setattr(reverify_x402, "_probe", probe)
    result = reverify_x402.reverify(
        targets=("mcp", "services"), workers=1
    )

    assert result["services_inserted"] == 0
    with closing(real_connect(str(database), read_only=True)) as conn:
        mcp = conn.execute(
            "SELECT endpoint_url,x402_supported FROM mcp_servers WHERE id=?",
            (mcp_id,),
        ).fetchone()
        assert mcp["endpoint_url"] == new_mcp
        assert mcp["x402_supported"] == 0
        service = conn.execute(
            "SELECT url,x402_ok,payment,resource_count,descriptor_checked_at "
            "FROM services WHERE id=?",
            (service_id,),
        ).fetchone()
        assert service["url"] == new_paid
        assert service["x402_ok"] in (None, 0)
        assert service["payment"] is None
        assert service["resource_count"] is None
        assert service["descriptor_checked_at"] is None


def test_crawl_refresh_does_not_hide_stale_descriptor(tmp_path, monkeypatch):
    database = tmp_path / "directory.db"
    monkeypatch.setenv("PRIVACY_SUPPRESSION_KEY", "privacy-test-key")
    db.init_db(str(database))
    with db.writer(str(database)) as conn:
        _, service_id = db.upsert_service(conn, {
            "slug": "crawled-service", "name": "Crawled",
            "url": "https://crawled.example.test/api",
            "source": "x402scan", "source_id": "crawled-service",
            "health": "ok", "payment": json.dumps({"pay_to": "0xabc"}),
        })
        conn.execute("UPDATE services SET x402_ok=1, health='ok' WHERE id=?",
                     (service_id,))
    real_connect = db.connect
    real_writer = db.writer
    monkeypatch.setattr(
        reverify_x402.db, "connect",
        lambda *_args, **kwargs: real_connect(
            str(database), read_only=kwargs.get("read_only", False)
        ),
    )
    monkeypatch.setattr(
        reverify_x402.db, "writer",
        lambda *_args, **kwargs: real_writer(
            str(database), immediate=kwargs.get("immediate", False)
        ),
    )

    real_gather = reverify_x402._gather

    def gathered():
        with closing(db.connect(str(database), read_only=True)) as conn:
            return [t["id"] for t in real_gather(
                conn, ("services",), None, False)]

    # A crawl upsert just bumped updated_at, but the descriptor was never read.
    assert gathered() == [service_id]

    task = {"table": "services", "id": service_id, "endpoint": "https://crawled.example.test/api",
            "verdict": {"status": "error", "payment": None}, "resources": {}}
    monkeypatch.setattr(reverify_x402, "_gather", lambda *a, **k: [task])
    monkeypatch.setattr(reverify_x402, "_probe", lambda t: t)
    reverify_x402.reverify(targets=("services",), workers=1)
    assert gathered() == []
