import io
import logging
import unittest

from directory import log_redact


class RedactTest(unittest.TestCase):
    def test_masks_known_query_secrets(self):
        cases = {
            "https://server.smithery.ai/a/b/mcp?api_key=8a58cfe0-daf2 \"HTTP/1.1 200 OK\"":
                "https://server.smithery.ai/a/b/mcp?api_key=*** \"HTTP/1.1 200 OK\"",
            "https://api.etherscan.io/v2/api?chainid=1&module=x&apikey=ABC123&page=2":
                "https://api.etherscan.io/v2/api?chainid=1&module=x&apikey=***&page=2",
            "https://x.test/p?apiKey=Z&token=T#frag": "https://x.test/p?apiKey=***&token=***#frag",
            "https://x.test/p?APIKEY=Z": "https://x.test/p?APIKEY=***",
            "https://x.test/p?key=AIza;access_token=q": "https://x.test/p?key=***;access_token=***",
        }
        for raw, expected in cases.items():
            self.assertEqual(log_redact.redact(raw), expected)

    def test_leaves_ordinary_params(self):
        text = "https://x.test/p?keyword=a&monkey=b&tokens_used=3&chainid=1"
        self.assertEqual(log_redact.redact(text), text)

    def test_httpx_style_record_through_root_handler(self):
        stream = io.StringIO()
        root = logging.getLogger()
        handler = logging.StreamHandler(stream)
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.INFO)
        try:
            log_redact.install()
            log_redact.install()  # idempotent
            logging.getLogger("httpx").info(
                'HTTP Request: %s %s "%s %d %s"', "POST",
                "https://server.smithery.ai/x/mcp?api_key=secret-value", "HTTP/1.1", 200, "OK")
            logging.getLogger("directory.other").info("fetch https://h.test/?token=%s", "abc")
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        out = stream.getvalue()
        self.assertNotIn("secret-value", out)
        self.assertNotIn("abc", out)
        self.assertIn("api_key=***", out)
        self.assertIn('"HTTP/1.1 200 OK"', out)
        self.assertEqual(logging.getLogger("httpx").filters.count(log_redact._FILTER), 1)


if __name__ == "__main__":
    unittest.main()
