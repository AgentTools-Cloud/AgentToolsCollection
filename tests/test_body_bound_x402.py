import base64
import json
import unittest
from unittest.mock import patch

from directory import crawlers


TASK_URL = "https://api.aomi.dev/v1/task/build"
EXAMPLE = {
    "intent": "Simulate a no-op on Arc Mainnet. Do not sign or broadcast.",
    "chainId": 5042,
    "sender": "0x000000000000000000000000000000000000dEaD",
}


class Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()
        self.headers = headers or {}


class Client:
    def __init__(self, get_responses, post_response):
        self.get_responses = iter(get_responses)
        self.post_response = post_response
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return next(self.get_responses)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_response


class BodyBoundProbeTests(unittest.TestCase):
    def test_documented_post_returns_arc_payment_terms(self):
        descriptor = {
            "x402Version": 2,
            "paths": {
                "/v1/task/build": {
                    "methods": ["POST"],
                    "payment": {
                        "endpoint": TASK_URL,
                        "paymentProtocol": "x402 v2",
                        "exampleRequest": EXAMPLE,
                    },
                },
            },
        }
        offer = {
            "x402Version": 2,
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:5042",
                "amount": "10000",
                "asset": "0x3600000000000000000000000000000000000000",
                "payTo": "0x1234567890123456789012345678901234567890",
                "extra": {"name": "GatewayWalletBatched"},
            }],
        }
        header = base64.b64encode(json.dumps(offer).encode()).decode()
        client = Client(
            [Response(200, descriptor), Response(404), Response(422), Response(404)],
            Response(402, headers={"payment-required": header}),
        )
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(TASK_URL)
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["payment"]["network"], "eip155:5042")
        self.assertEqual(verdict["payment"]["max_amount_usdc"], 0.01)
        self.assertEqual(client.posts, [(TASK_URL, {
            "json": EXAMPLE,
            "follow_redirects": False,
        })])

    def test_unrelated_manifest_does_not_trigger_post(self):
        descriptor = {
            "x402Version": 2,
            "paths": {
                "/v1/task/build": {
                    "methods": ["POST"],
                    "payment": {
                        "endpoint": "https://elsewhere.example/v1/task/build",
                        "paymentProtocol": "x402 v2",
                        "exampleRequest": EXAMPLE,
                    },
                },
            },
        }
        client = Client(
            [Response(200, descriptor), Response(404), Response(422), Response(404)],
            Response(402),
        )
        with (
            patch.object(crawlers, "_host_safety", return_value="public"),
            patch.object(crawlers.httpx, "Client", return_value=client),
        ):
            verdict = crawlers.verify_x402(TASK_URL)
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(client.posts, [])


if __name__ == "__main__":
    unittest.main()
