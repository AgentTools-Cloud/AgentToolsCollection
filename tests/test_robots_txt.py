import unittest

from directory import routes


class RobotsTxtTests(unittest.TestCase):
    def test_owner_only_paths_disallowed_public_allowed(self):
        body = routes.robots_txt().body.decode()
        for path in ("/auth/", "/listings/*/edit", "/account"):
            self.assertIn(f"Disallow: {path}\n", body)
        for public in ("/services/", "/mcp/servers/", "/a2a/agents/", "/api/v1/search", "/api/v1/export",
                       "/listings/", "/api/v1/listings/"):
            self.assertFalse(any(public.startswith(line.split(": ", 1)[1].split("*")[0]) and "*" not in line
                                 for line in body.splitlines() if line.startswith("Disallow: ")), public)
        self.assertIn("Sitemap: https://agent-tools.cloud/sitemap.xml", body)


if __name__ == "__main__":
    unittest.main()
