"""Browser sign-in without GitHub: access-key accounts, key sign-in, optional email."""
import os
import re
from contextlib import closing
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from directory import auth, db, mailer, routes
from test_ingestion_pipeline import TemporaryDatabaseTestCase

SITE = "https://agent-tools.cloud"
SAME = {"origin": SITE}


class WebSigninTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {
            "SESSION_SECRET": "test-secret-not-production",
            "PUBLIC_BASE_URL": SITE,
            "GITHUB_OAUTH_CLIENT_ID": "fixture-id",
            "GITHUB_OAUTH_CLIENT_SECRET": "fixture-secret",
            "HUB_COOKIE_DOMAIN": "",
        }))
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.include_router(auth.router)

    def client(self):
        return TestClient(self.app, base_url=SITE)

    def users(self):
        return self.query("SELECT * FROM users ORDER BY id")

    def start(self, **form):
        client = self.client()
        response = client.post("/auth/anonymous", data={"next": "/account", **form},
                               headers=SAME, follow_redirects=False)
        return client, response

    def signed_in(self):
        client, response = self.start()
        self.assertEqual(response.status_code, 303, response.text)
        key = re.search(r"atc_live_[A-Za-z0-9_-]+", client.get("/account").text).group(0)
        return client, key

    def test_signed_out_pages_lead_to_the_sign_in_choice(self):
        client = self.client()
        response = client.get("/account?host=Example.COM", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        location = urlsplit(response.headers["location"])
        self.assertEqual(location.path, "/auth/signin")
        self.assertEqual(parse_qs(location.query),
                         {"next": ["/account?host=Example.COM"], "host": ["example.com"]})
        for path in ("/listings/mcp/some-server/edit", "/listings/mcp/some-server/manage"):
            with self.subTest(path=path):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"],
                                 "/auth/signin?" + urlencode({"next": path}))

        page = client.get("/auth/signin?next=/account&host=example.com")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "private, no-store")
        self.assertIn("noindex", page.headers["x-robots-tag"])
        self.assertIn('action="/auth/anonymous"', page.text)
        self.assertIn('action="/auth/key"', page.text)
        self.assertIn('name="host" value="example.com"', page.text)
        github = re.search(r'href="/auth/github/login\?next=([^"]+)"', page.text)
        self.assertEqual(unquote(github.group(1)), "/account?host=example.com")
        self.assertEqual(self.users(), [])

    def test_continue_without_an_account_creates_a_key_account_and_shows_the_key_once(self):
        client, response = self.start(host="Example.com")
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(response.headers["location"], "/account?host=example.com")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        (user,) = self.users()
        self.assertEqual(user["provider"], "domain")
        self.assertTrue(user["login"].startswith("agent-"))

        page = client.get(response.headers["location"])
        self.assertEqual(page.status_code, 200, page.text)
        key = re.search(r"atc_live_[A-Za-z0-9_-]+", page.text).group(0)
        with closing(db.connect(read_only=True)) as conn:
            self.assertEqual(db.api_key_row(conn, key)["user_id"], user["id"])
        self.assertIn("access-key account (no GitHub)", page.text)
        self.assertIn('value="example.com"', page.text)
        self.assertNotIn(key, client.get("/account").text)

        # The key alone signs a fresh browser into the same account ...
        other = self.client()
        response = other.post("/auth/key", data={"key": key, "next": "/account"},
                              headers=SAME, follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/account"))
        self.assertIn(user["login"], other.get("/account").text)
        # ... and is the same Bearer key the API accepts.
        me = self.client().get("/api/v1/me", headers={"Authorization": "Bearer " + key}).json()
        self.assertTrue(me["signed_in"])
        self.assertEqual(len(self.users()), 1)

    def test_signed_in_visitors_skip_the_choice(self):
        client, _ = self.signed_in()
        response = client.get("/auth/signin?next=/docs/claim", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (302, "/docs/claim"))

    def test_an_account_without_github_opens_claims_on_the_web(self):
        client, _ = self.signed_in()
        url = "https://glama.ai/endpoints/profile-123/mcp"
        response = client.post("/account/service-claims", data={"url": url}, headers=SAME)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(url, response.text)
        self.assertIn("/account/service-claims/", response.text)
        (claim,) = self.query("SELECT user_id, status FROM domain_ownership")
        self.assertEqual(claim, {"user_id": self.users()[0]["id"], "status": "pending"})

    def test_github_accounts_still_say_so(self):
        with db.writer() as conn:
            uid = db.upsert_user(conn, provider="github", provider_uid="42", login="octo",
                                 email="octo@example.com", email_verified=True)
        client = self.client()
        client.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": uid}, 3600))
        page = client.get("/account")
        self.assertIn("signed in with GitHub", page.text)
        self.assertNotIn('action="/account/email"', page.text)

    def test_sign_in_forms_refuse_other_sites(self):
        client = self.client()
        for path, data in (("/auth/anonymous", {}), ("/auth/key", {"key": "atc_live_x"})):
            for headers in ({}, {"origin": "https://evil.example"},
                            {"referer": "https://evil.example/page"}):
                with self.subTest(path=path, headers=headers):
                    response = client.post(path, data=data, headers=headers,
                                           follow_redirects=False)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn("atc_session", response.headers.get("set-cookie", ""))
        self.assertEqual(self.users(), [])

    def test_new_accounts_share_the_api_key_budget(self):
        with patch.object(routes, "KEY_MINT_LIMIT_PER_DAY", 2):
            self.assertEqual(self.client().post("/api/v1/keys", json={}).status_code, 201)
            self.assertEqual(self.start()[1].status_code, 303)
            _, response = self.start()
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too many accounts", response.text)
        self.assertNotIn("atc_session", response.headers.get("set-cookie", ""))
        self.assertEqual(len(self.users()), 2)

    def test_a_wrong_key_does_not_sign_in(self):
        response = self.client().post("/auth/key", data={"key": "atc_live_nope"},
                                      headers=SAME, follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        self.assertIn("That key is not valid", response.text)
        self.assertNotIn("atc_session", response.headers.get("set-cookie", ""))

    def test_optional_email_is_confirmed_by_a_post_from_the_link(self):
        client, _ = self.signed_in()
        sent = []
        with patch.object(mailer, "send_email_confirmation",
                          side_effect=lambda *args: sent.append(args) or True):
            response = client.post("/account/email", data={"email": "Owner@Example.com"},
                                   headers=SAME)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("We sent a confirmation link to Owner@Example.com", response.text)
        ((to, link, account),) = sent
        self.assertEqual((to, account), ("Owner@Example.com", self.users()[0]["login"]))
        self.assertTrue(link.startswith(SITE + "/account/email/confirm#"))
        token = link.split("#", 1)[1]
        self.assertIsNone(self.users()[0]["email"])

        # Opening the link (or a mail scanner doing so) changes nothing.
        page = self.client().get("/account/email/confirm")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["referrer-policy"], "same-origin")
        self.assertIsNone(self.users()[0]["email"])
        # The token is not a session.
        stranger = self.client()
        stranger.cookies.set(auth.SESSION_COOKIE, token)
        self.assertEqual(stranger.get("/account", follow_redirects=False).status_code, 302)
        # It confirms from any browser, but only through our own page.
        self.assertEqual(self.client().post("/account/email/confirm", data={"token": token},
                                            headers={"origin": "https://evil.example"}).status_code, 403)
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertEqual(self.client().post("/account/email/confirm", data={"token": tampered},
                                            headers=SAME).status_code, 400)
        self.assertEqual(self.client().post("/account/email/confirm", data={"token": "é"},
                                            headers=SAME).status_code, 400)
        done = self.client().post("/account/email/confirm", data={"token": token}, headers=SAME)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertIn("Email confirmed", done.text)
        user = self.users()[0]
        self.assertEqual((user["email"], user["email_verified"]), ("Owner@Example.com", 1))
        page = client.get("/account").text
        self.assertIn("Owner@Example.com", page)
        self.assertNotIn('action="/account/email"', page)

    def test_confirmation_mail_is_validated_and_limited_per_account(self):
        client, _ = self.signed_in()
        with patch.object(mailer, "send_email_confirmation", return_value=True) as send:
            for bad in ("not an email", "a@b.example,c@d.example", "a@b.example\r\nBcc: c@d.example"):
                with self.subTest(email=bad):
                    self.assertEqual(client.post("/account/email", data={"email": bad},
                                                 headers=SAME).status_code, 400)
            codes = [client.post("/account/email", data={"email": "x%d@example.com" % i},
                                 headers=SAME).status_code for i in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(send.call_count, 3)

    def test_confirmation_mail_is_limited_per_recipient(self):
        first, _ = self.signed_in()
        second, _ = self.signed_in()
        with patch.object(mailer, "send_email_confirmation", return_value=True) as send:
            codes = [client.post("/account/email", data={"email": "same@example.com"},
                                 headers=SAME).status_code for client in (first, second, second)]
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual(send.call_count, 2)

    def test_mail_copy(self):
        _, text, markup = mailer.claim_invitation_message(
            "Svc", "the MCP Registry", SITE + "/mcp/servers/svc",
            SITE + "/email/claim-invitations/unsubscribe#t")
        for body in (text, markup):
            self.assertIn("Sign in with GitHub or continue without an account", body)
        subject, text, markup = mailer.email_confirmation_message(
            SITE + "/account/email/confirm#tok", "agent-<b>")
        self.assertEqual(subject, "Confirm your email for Agent Tools")
        self.assertIn(SITE + "/account/email/confirm#tok", text)
        self.assertIn("agent-&lt;b&gt;", markup)
        self.assertNotIn("agent-<b>", markup)


if __name__ == "__main__":
    import unittest
    unittest.main()
