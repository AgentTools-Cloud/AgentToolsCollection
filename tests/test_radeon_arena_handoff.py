"""Radeon Arena SSO handoff uses a short-lived audience-bound one-use assertion."""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time

os.environ["SESSION_SECRET"] = "test-session-secret-not-production"
os.environ["RADEON_ARENA_ACCOUNT_SECRET"] = "test-arena-secret-not-production"
DB = tempfile.mktemp(suffix=".db")
os.environ["AGENT_TOOLS_DB_PATH"] = DB

from directory import auth, db  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def decode(assertion: str) -> dict:
    encoded, signature = assertion.rsplit(".", 1)
    expected = hmac.new(
        os.environ["RADEON_ARENA_ACCOUNT_SECRET"].encode(), encoded.encode(), hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(signature, expected)
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


db.init_db(DB)
with db.writer() as conn:
    uid = db.upsert_user(
        conn, provider="github", provider_uid="42", login="arena-user",
        avatar_url="https://avatars.example/user.png", email="verified@example.com",
        email_verified=True,
    )

app = FastAPI()
app.include_router(auth.router)
client = TestClient(app, base_url="https://agent-tools.cloud")

anonymous = client.get("/auth/radeon-arena", params={"next": "https://evil.example"},
                       follow_redirects=False)
assert anonymous.status_code == 302
assert anonymous.headers["location"].startswith("/auth/github/login?")
assert "evil.example" not in anonymous.headers["location"]

client.cookies.set(auth.SESSION_COOKIE, auth.sign({"uid": uid}, 3600))
response = client.get("/auth/radeon-arena", params={"next": "/device?code=ABCD-EFGH"})
assert response.status_code == 200
assert response.headers["cache-control"] == "private, no-store"
assert response.headers["x-robots-tag"] == "noindex, nofollow"
assert "action=https://radeon-arena.com/api/auth/sso" in response.text
assertion = response.text.split('name=assertion value="', 1)[1].split('"', 1)[0]
payload = decode(assertion)
assert payload["aud"] == "radeon-arena.com"
assert payload["sub"] == "github:42"
assert payload["login"] == "arena-user"
assert payload["email"] == "verified@example.com"
assert payload["next"] == "/device?code=ABCD-EFGH"
assert payload["jti"]
assert 0 < payload["exp"] - int(time.time()) <= 120

with db.writer() as conn:
    conn.execute("UPDATE users SET email_verified=0 WHERE id=?", (uid,))
response = client.get("/auth/radeon-arena")
assert decode(response.text.split('name=assertion value="', 1)[1].split('"', 1)[0])["email"] is None

os.environ.pop("RADEON_ARENA_ACCOUNT_SECRET")
assert client.get("/auth/radeon-arena").status_code == 503
os.unlink(DB)
print("Radeon Arena SSO handoff tests passed")
