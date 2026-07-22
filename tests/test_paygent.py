import tempfile
import unittest
from pathlib import Path

from directory import db, paygent


class PaygentOriginTests(unittest.TestCase):
    def test_resources_are_aggregated_by_origin(self):
        seen = set()
        resources = [
            {
                "id": "resource-one",
                "kind": "mpp",
                "resource": "https://deeptrawler.com/v1/telegram/search",
                "operator": "DeepTrawler",
                "networks": ["base"],
                "currencies": ["USDC"],
                "reputation": {"composite": 75, "confidence": 0.9},
            },
            {
                "id": "resource-two",
                "kind": "x402",
                "resource": "https://deeptrawler.com/v1/telegram/search/public-posts",
                "operator": "DeepTrawler",
                "networks": ["base"],
                "currencies": ["USDC"],
                "reputation": {"composite": 80, "confidence": 0.95},
            },
        ]

        rows = paygent._aggregate_origins(
            [row for item in resources if (row := paygent._map(item, seen))]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://deeptrawler.com")
        self.assertEqual(rows[0]["source_id"], "origin:https://deeptrawler.com")
        self.assertEqual(rows[0]["resource_count"], 2)
        self.assertEqual(len(rows[0]["resource_samples"]), 2)

    def test_stable_origin_upsert_reuses_legacy_service_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "directory.db")
            db.init_db(db_path)
            with db.writer(db_path) as conn:
                created, legacy_id = db.upsert_service(
                    conn,
                    {
                        "slug": "deeptrawler-com-pg-resource",
                        "name": "deeptrawler.com",
                        "url": "https://deeptrawler.com",
                        "source": "paygent-discover",
                        "source_id": "legacy-resource-id",
                        "resource_count": 1,
                        "resource_samples": [
                            {
                                "url": "https://deeptrawler.com/v1/telegram/search",
                                "kind": "mpp-resource",
                            }
                        ],
                    },
                )
                self.assertTrue(created)

                created, stable_id = db.upsert_service(
                    conn,
                    {
                        "slug": "deeptrawler-com-pg-b0479b84",
                        "name": "DeepTrawler",
                        "url": "https://deeptrawler.com/",
                        "source": "paygent-discover",
                        "source_id": "origin:https://deeptrawler.com",
                        "resource_count": 2,
                        "resource_samples": [
                            {
                                "url": "https://deeptrawler.com/v1/telegram/search",
                                "kind": "mpp-resource",
                            },
                            {
                                "url": "https://deeptrawler.com/v1/telegram/search/public-posts",
                                "kind": "x402-resource",
                            },
                        ],
                    },
                )

                self.assertFalse(created)
                self.assertEqual(stable_id, legacy_id)
                row = conn.execute(
                    "SELECT COUNT(*) AS n, MIN(id) AS id, MIN(source_id) AS source_id "
                    "FROM services"
                ).fetchone()
                self.assertEqual(row["n"], 1)
                self.assertEqual(row["id"], legacy_id)
                self.assertEqual(row["source_id"], "origin:https://deeptrawler.com")


if __name__ == "__main__":
    unittest.main()