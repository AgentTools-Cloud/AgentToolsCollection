import json
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from directory import auth, db, mcp_pricing, resources, routes
from test_ingestion_pipeline import TemporaryDatabaseTestCase


ROOT = Path(__file__).resolve().parents[1]


def render_schema(filename, variable, **values):
    source = (ROOT / "directory/templates" / filename).read_text(encoding="utf-8")
    fragment = source.split("{% block head_extra %}", 1)[1].split("{% endblock %}", 1)[0]
    item = {"name": "Offline pricing fixture", "slug": "offline-pricing-fixture",
            "description": "Free discovery; buyer-approved Website Check costs EUR 9.90",
            "cost_hint": None, "auth_method": None, **values}
    rendered = Environment(autoescape=True).from_string(fragment).render(**{variable: item})
    return json.loads(rendered.split(">", 1)[1].rsplit("</script>", 1)[0])


class SchemaPricingTests(unittest.TestCase):
    def test_unknown_price_is_not_claimed_free(self):
        for filename, variable in (("mcp_server.html", "m"), ("a2a_agent.html", "a")):
            with self.subTest(filename=filename):
                self.assertNotIn("offers", render_schema(filename, variable))

    def test_unstructured_cost_does_not_invent_numeric_price(self):
        for filename, variable in (("mcp_server.html", "m"), ("a2a_agent.html", "a")):
            with self.subTest(filename=filename):
                schema = render_schema(filename, variable, cost_hint="Free discovery; paid operation EUR 9.90")
                self.assertNotIn("offers", schema)

    def test_explicit_operations_keep_amount_currency_and_approval(self):
        pricing = mcp_pricing.normalize({"buyer_approval_required": True, "offers": [
            {"name": "MCP discovery", "price": "0", "priceCurrency": "EUR"},
            {"name": "Website Check", "price": "9.90", "priceCurrency": "EUR", "description": "Buyer-approved MPP or Stripe Checkout only"},
        ]})
        schema = render_schema("mcp_server.html", "m", pricing=pricing)
        self.assertEqual([(offer["price"], offer["priceCurrency"]) for offer in schema["offers"]], [("0", "EUR"), ("9.90", "EUR")])
        self.assertTrue(pricing["buyer_approval_required"])

    def test_invalid_or_ambiguous_declarations_are_rejected(self):
        offer = {"name": "Check", "price": "9.90", "priceCurrency": "EUR"}
        for changes in ({"price": -1}, {"price": True}, {"price": "Infinity"}, {"price": float("inf")}, {"price": float("nan")}, {"price": 0.000000001}, {"price": 1000001.0}, {"price": "1e309"}, {"price": None}, {"priceCurrency": "USDC"}, {"priceCurrency": "eur"}, {"url": "https://unapproved.example/pay"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                mcp_pricing.normalize({"offers": [{**offer, **changes}]})
        with self.assertRaises(ValueError):
            mcp_pricing.normalize({"offers": [offer] * 21})

    def test_pricing_is_bound_to_the_current_endpoint(self):
        declaration = {"offers": [{"name": "Check", "price": "9.90", "priceCurrency": "EUR"}]}
        stored = mcp_pricing.serialize(declaration, "https://public.example/mcp")
        self.assertEqual(mcp_pricing.read(stored, "https://public.example/mcp")["offers"][0]["price"], "9.90")
        self.assertIsNone(mcp_pricing.read(stored, "https://changed.example/mcp"))
        self.assertIsNone(mcp_pricing.read("invalid", "https://public.example/mcp"))

    def test_auth_names_are_not_credentials_or_payment(self):
        self.assertEqual(mcp_pricing.normalize_auth("none"), "none")
        with self.assertRaises(ValueError):
            mcp_pricing.normalize_auth("Bearer private-credential")
        with self.assertRaises(ValueError):
            mcp_pricing.normalize_auth("buyer_approval")
        self.assertEqual(mcp_pricing.normalize_auth("legacy_scheme", "legacy_scheme"), "legacy_scheme")


class OwnerPricingTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        with db.writer() as connection:
            self.user_id = connection.execute("INSERT INTO users(provider,provider_uid,login,status,created_at) VALUES('github','pricing-fixture','pricing-fixture','active',1)").lastrowid
            self.claim_id = connection.execute("INSERT INTO domain_ownership(user_id,host,method,token_hash,status,created_at,verified_at) VALUES(?,'owned.example','dns','fixture','verified',1,1)", (self.user_id,)).lastrowid
            _, self.listing_id = db.upsert_mcp_server(connection, {
                "slug": "owned-pricing", "name": "Owned pricing", "description": "Owner description must stay",
                "endpoint_url": "https://owned.example/mcp", "source": "fixture", "source_id": "owned-pricing", "health": "ok",
            })
            connection.execute("UPDATE mcp_servers SET owner_verified=1,quality_score=90,tool_count=2,conformance='pass' WHERE id=?", (self.listing_id,))
        self.declaration = {"buyer_approval_required": True, "offers": [
            {"name": "MCP discovery", "price": "0", "priceCurrency": "EUR"},
            {"name": "Website Check", "price": "9.90", "priceCurrency": "EUR", "description": "Buyer-approved MPP or Stripe Checkout only"},
        ]}

    def edit(self, changes):
        with db.writer(immediate=True) as connection:
            return db.apply_listing_edits(connection, "mcp", self.listing_id, self.user_id, self.claim_id, changes)

    def row(self):
        with closing(db.connect(read_only=True)) as connection:
            return db.get_mcp_by_slug(connection, "owned-pricing")

    def test_owner_can_save_cost_auth_and_structured_operations(self):
        applied, rejected = self.edit({"cost_hint": "Free discovery; EUR 9.90 buyer-approved check", "auth_method": "none", "pricing": self.declaration})
        self.assertFalse(rejected)
        self.assertEqual(set(applied), {"cost_hint", "auth_method", "pricing"})
        row = self.row()
        self.assertEqual(row["auth_method"], "none")
        self.assertEqual(row["pricing"]["offers"][1]["priceCurrency"], "EUR")
        self.assertEqual((row["description"], row["quality_score"], row["conformance"], row["tool_count"]), ("Owner description must stay", 90, "pass", 2))
        self.assertEqual(set(json.loads(row["owner_edited"])), {"cost_hint", "auth_method", "pricing"})

    def test_crawler_refresh_keeps_owned_cost_auth_and_pricing(self):
        self.edit({"cost_hint": "EUR 9.90 buyer-approved check", "auth_method": "none", "pricing": self.declaration})
        before = self.row()
        with db.writer() as connection:
            connection.execute("UPDATE mcp_servers SET owner_verified=0 WHERE id=?", (self.listing_id,))
            db.upsert_mcp_server(connection, {"slug": "owned-pricing", "name": "Crawler name", "endpoint_url": "https://owned.example/mcp", "source": "fixture", "source_id": "owned-pricing", "cost_hint": None, "auth_method": None})
        after = self.row()
        for field in ("cost_hint", "auth_method", "pricing"):
            self.assertEqual(after[field], before[field], field)

    def test_invalid_pricing_or_credentials_cannot_partially_change_description(self):
        for changes in ({"pricing": {"offers": [{"name": "Check", "price": "NaN", "priceCurrency": "EUR"}]}}, {"auth_method": "Bearer private-token"}):
            applied, rejected = self.edit({"description": "must not apply", **changes})
            self.assertFalse(applied)
            self.assertTrue(rejected)
            self.assertEqual(self.row()["description"], "Owner description must stay")

    def test_revoked_claim_cannot_edit_pricing(self):
        with db.writer() as connection:
            connection.execute("UPDATE domain_ownership SET status='revoked' WHERE id=?", (self.claim_id,))
        with self.assertRaises(PermissionError):
            self.edit({"pricing": self.declaration})

    def test_new_endpoint_does_not_reuse_old_pricing(self):
        self.edit({"pricing": self.declaration})
        applied, rejected = self.edit({"endpoint_url": "https://owned.example/v2/mcp"})
        self.assertFalse(rejected)
        self.assertIn("endpoint_url", applied)
        self.assertIsNone(self.row()["pricing"])
        self.assertIsNone(self.query("SELECT pricing FROM mcp_servers WHERE id=?", (self.listing_id,))[0]["pricing"])
        self.edit({"endpoint_url": "https://owned.example/mcp"})
        self.assertIsNone(self.row()["pricing"])

    def test_endpoint_form_with_empty_pricing_remains_editable(self):
        applied, rejected = self.edit({"endpoint_url": "https://owned.example/v2/mcp", "pricing": ""})
        self.assertFalse(rejected)
        self.assertIn("endpoint_url", applied)
        self.assertIsNone(self.row()["pricing"])

    def test_endpoint_form_invalidates_unchanged_existing_pricing(self):
        self.edit({"pricing": self.declaration})
        applied, rejected = self.edit({"endpoint_url": "https://owned.example/v2/mcp", "pricing": json.dumps(self.declaration)})
        self.assertFalse(rejected)
        self.assertEqual(set(applied), {"endpoint_url", "pricing"})
        self.assertIsNone(self.row()["pricing"])

    def test_new_endpoint_and_new_pricing_require_separate_saves(self):
        applied, rejected = self.edit({"endpoint_url": "https://owned.example/v2/mcp", "pricing": self.declaration})
        self.assertFalse(applied)
        self.assertTrue(rejected)
        self.assertEqual(self.row()["endpoint_url"], "https://owned.example/mcp")
        self.assertIsNone(self.row()["pricing"])

    def test_public_api_and_unified_resource_preserve_buyer_approved_eur(self):
        self.edit({"cost_hint": "Free discovery; EUR 9.90 buyer-approved check", "auth_method": "none", "pricing": self.declaration})
        public = routes.api_mcp_server("owned-pricing")
        self.assertEqual(public["pricing"]["offers"][1]["price"], "9.90")
        item = resources.normalize_mcp_server(self.row())
        self.assertEqual(item["protocols"], ["mcp"])
        self.assertIsNone(item["price_hint"])
        self.assertTrue(item["access"]["paid"])
        self.assertTrue(item["access"]["buyer_approval_required"])
        self.assertFalse(item["access"]["agent_can_self_serve"])

    def test_edit_context_exposes_cost_auth_and_json_without_payment_verification(self):
        self.edit({"pricing": self.declaration})
        context = auth._edit_context(None, "mcp", "owned-pricing", {"id": self.user_id}, self.row(), "owned.example")
        fields = {field["name"]: field for field in context["fields"]}
        self.assertTrue(fields["pricing"]["multiline"])
        self.assertTrue(fields["cost_hint"]["multiline"])
        self.assertTrue(fields["auth_method"]["choices"])
        self.assertEqual(json.loads(fields["pricing"]["value"])["offers"][1]["priceCurrency"], "EUR")
        self.assertIsNone(context["declaration"])

    def test_editor_renders_cost_auth_select_and_pricing_json(self):
        self.edit({"cost_hint": "Free discovery; EUR 9.90 buyer-approved check", "auth_method": "none", "pricing": self.declaration})
        context = auth._edit_context(None, "mcp", "owned-pricing", {"id": self.user_id}, self.row(), "owned.example")
        loader = ChoiceLoader([DictLoader({"base.html": "<html><body>{% block content %}{% endblock %}</body></html>"}), FileSystemLoader(ROOT / "directory/templates")])
        markup = Environment(loader=loader, autoescape=True).get_template("edit_listing.html").render(**context)
        self.assertIn('name="cost_hint"', markup)
        self.assertIn('<select name="auth_method"', markup)
        self.assertIn('value="none" selected', markup)
        self.assertIn('name="pricing"', markup)
        self.assertIn('maxlength="32768"', markup)
        self.assertIn('9.90', markup)

    def test_existing_database_migration_is_idempotent(self):
        with db.writer() as connection:
            connection.execute("ALTER TABLE mcp_servers DROP COLUMN pricing")
        db.init_db(self.path)
        db.init_db(self.path)
        self.assertEqual(sum(row["name"] == "pricing" for row in self.query("PRAGMA table_info(mcp_servers)")), 1)

    def review_http_client(self):
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(routes.router)
        return TestClient(app)

    def review_owner_headers(self):
        with db.writer() as connection:
            key = db.create_api_key(connection, self.user_id, name="pricing-review")
        return {"Authorization": "Bearer " + key}

    def test_review_complete_owner_api_and_form_roundtrip(self):
        headers = self.review_owner_headers()
        with self.review_http_client() as client:
            saved = client.patch("/api/v1/listings/mcp/owned-pricing", headers=headers, json={
                "cost_hint": "Free discovery; EUR 9.90 buyer-approved operation",
                "auth_method": "none", "pricing": self.declaration})
            self.assertEqual(saved.status_code, 200, saved.text)
            private = client.get("/api/v1/listings/mcp/owned-pricing", headers=headers)
            self.assertEqual(private.status_code, 200, private.text)
            self.assertIn("no-store", private.headers["cache-control"])
            fields = private.json()["fields"]
            self.assertEqual(fields["pricing"]["offers"][1]["price"], "9.90")
            editor = client.get("/listings/mcp/owned-pricing/edit", headers=headers)
            self.assertEqual(editor.status_code, 200, editor.text)
            self.assertIn('name="pricing"', editor.text)
            form = {name: value or "" for name, value in fields.items()}
            form["pricing"] = json.dumps(fields["pricing"])
            posted = client.post("/listings/mcp/owned-pricing/edit", headers=headers, data=form)
            self.assertEqual(posted.status_code, 200, posted.text)
            public = client.get("/api/v1/mcp/servers/owned-pricing")
            self.assertEqual(public.json()["pricing"], fields["pricing"])
        self.assertEqual((self.row()["quality_score"], self.row()["tool_count"]), (90, 2))

    def test_review_unclaimed_key_cannot_edit_pricing(self):
        with db.writer() as connection:
            other_id = connection.execute("INSERT INTO users(provider,provider_uid,login,status,created_at) VALUES('github','pricing-other','pricing-other','active',1)").lastrowid
            key = db.create_api_key(connection, other_id, name="pricing-review-other")
        with self.review_http_client() as client:
            response = client.patch("/api/v1/listings/mcp/owned-pricing", headers={"Authorization": "Bearer " + key}, json={"pricing": self.declaration})
            self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNone(self.row()["pricing"])

    def test_review_cross_origin_cookie_cannot_edit_pricing(self):
        with patch.dict("os.environ", {"SESSION_SECRET": "pricing-review-signing-only"}), self.review_http_client() as client:
            client.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": self.user_id}, 60))
            response = client.patch("/api/v1/listings/mcp/owned-pricing", headers={"Origin": "https://foreign.example"}, json={"pricing": self.declaration})
            self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNone(self.row()["pricing"])

    def test_review_invalid_pricing_does_not_half_write_via_http(self):
        headers = self.review_owner_headers()
        with self.review_http_client() as client:
            response = client.patch("/api/v1/listings/mcp/owned-pricing", headers=headers, json={
                "description": "must not apply", "pricing": {"offers": [{"name": "Operation", "price": -1, "priceCurrency": "EUR"}]}})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.row()["description"], "Owner description must stay")
        self.assertIsNone(self.row()["pricing"])

    def test_review_equivalent_endpoint_keeps_pricing(self):
        self.edit({"pricing": self.declaration})
        before = self.row()
        with self.review_http_client() as client:
            response = client.patch("/api/v1/listings/mcp/owned-pricing", headers=self.review_owner_headers(), json={"endpoint_url": "https://OWNED.EXAMPLE/mcp"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["applied"], [])
        after = self.row()
        self.assertEqual(after["endpoint_url"], before["endpoint_url"])
        self.assertEqual(after["pricing"], before["pricing"])
        self.assertEqual(after["quality_score"], before["quality_score"])
        self.assertEqual(after["tool_count"], before["tool_count"])
        self.assertEqual(after["owner_edited"], before["owner_edited"])

    def test_review_free_declaration_does_not_override_x402_access(self):
        with db.writer() as connection:
            connection.execute("UPDATE mcp_servers SET x402_supported=1 WHERE id=?", (self.listing_id,))
        declaration = {"offers": [{"name": "Discovery", "price": "0", "priceCurrency": "EUR"}], "buyer_approval_required": True}
        with self.review_http_client() as client:
            response = client.patch("/api/v1/listings/mcp/owned-pricing", headers=self.review_owner_headers(), json={"pricing": declaration})
            self.assertEqual(response.status_code, 200, response.text)
            response = client.get("/api/v1/resources/search", params={"q": "Owned pricing", "protocol": "mcp"})
            self.assertEqual(response.status_code, 200, response.text)
            items = next(value for value in response.json().values() if isinstance(value, list) and any(isinstance(item, dict) and item.get("slug") == "owned-pricing" for item in value))
            access = next(item["access"] for item in items if item["slug"] == "owned-pricing")
        self.assertEqual(self.row()["x402_supported"], 1)
        self.assertEqual(access["model"], "agent_pays")
        self.assertTrue(access["paid"], access)
        self.assertEqual(access["billing"], "x402-per-call")
        self.assertTrue(access["buyer_approval_required"])
        self.assertFalse(access["agent_can_self_serve"])

    def test_review_key_required_label_stays_key_required(self):
        with self.review_http_client() as client:
            response = client.patch("/api/v1/listings/mcp/owned-pricing", headers=self.review_owner_headers(), json={"auth_method": "api_key", "pricing": self.declaration})
            self.assertEqual(response.status_code, 200, response.text)
        access = resources.normalize_mcp_server(self.row())["access"]
        self.assertTrue(access["registration_required"])
        self.assertEqual(access["model"], "human_key")
        self.assertNotIn("Public discovery", access["label"])

    def test_review_small_decimal_json_number_is_supported(self):
        headers = {**self.review_owner_headers(), "Content-Type": "application/json"}
        with self.review_http_client() as client:
            for price, payload in (
                ("0.00001", '{"pricing":{"offers":[{"name":"Operation","price":0.00001,"priceCurrency":"USD"}],"buyer_approval_required":true}}'),
                ("0.00000001", '{"pricing":{"offers":[{"name":"Operation","price":0.00000001,"priceCurrency":"USD"}],"buyer_approval_required":true}}'),
            ):
                with self.subTest(price=price):
                    string_value = client.patch("/api/v1/listings/mcp/owned-pricing", headers=headers, json={"pricing": {"offers": [{"name": "Operation", "price": price, "priceCurrency": "USD"}], "buyer_approval_required": True}})
                    self.assertEqual(string_value.status_code, 200, string_value.text)
                    number_value = client.patch("/api/v1/listings/mcp/owned-pricing", headers=headers, content=payload)
                    self.assertEqual(number_value.status_code, 200, number_value.text)
                    self.assertEqual(self.row()["pricing"]["offers"][0]["price"], price)

if __name__ == "__main__":
    unittest.main()