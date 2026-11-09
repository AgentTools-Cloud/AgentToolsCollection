import json
from contextlib import closing
from pathlib import Path
import unittest

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
        for changes in ({"price": -1}, {"price": True}, {"price": "Infinity"}, {"price": "1e309"}, {"price": None}, {"priceCurrency": "USDC"}, {"priceCurrency": "eur"}, {"url": "https://unapproved.example/pay"}):
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


if __name__ == "__main__":
    unittest.main()