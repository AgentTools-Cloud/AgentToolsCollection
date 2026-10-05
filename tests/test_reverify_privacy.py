from contextlib import closing
import json

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