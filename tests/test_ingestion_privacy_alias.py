import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from directory import db, ingestion, wellknown_binding


class AliasPrivacyTests(unittest.TestCase):
    def test_alias_is_suppressed_after_purge_even_after_host_move(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'PRIVACY_SUPPRESSION_KEY': 'test-alias-privacy-key'}):
            path = str(Path(tmp) / 'test.db')
            db.init_db(path)
            with db.writer(path) as conn:
                _, lid = db.upsert_mcp_server(conn, {
                    'name': 'Example', 'slug': 'example', 'source': 'fixture',
                    'source_id': 'primary', 'endpoint_url': 'https://original.example.org/mcp',
                })
                row = {'name': 'Alias', 'slug': 'alias', 'source': 'wellknown',
                       'source_id': 'upstream-alias', 'endpoint_url': 'https://original.example.org/mcp'}
                wellknown_binding.upsert_listing(conn, 'mcp', row)
                self.assertEqual(conn.execute('SELECT count(*) FROM wellknown_endpoint_sources').fetchone()[0], 1)
                db.privacy_purge_listing(conn, 'mcp', 'example', 'test')
                self.assertEqual(conn.execute('SELECT count(*) FROM wellknown_endpoint_sources').fetchone()[0], 0)
                row['endpoint_url'] = 'https://moved.example.net/mcp'
                row['slug'] = 'new-slug'
                with self.assertRaises(db.RetiredListingError):
                    wellknown_binding.upsert_listing(conn, 'mcp', row)

    def test_relative_endpoint_only_resolves_within_card_origin(self):
        from directory.a2a import _card_endpoint
        base = 'https://example.org/.well-known/agent-card.json'
        self.assertEqual(_card_endpoint({'endpoint': '/mcp'}, base), 'https://example.org/mcp')
        self.assertEqual(_card_endpoint({'endpoint': 'POST /mcp'}, base), 'POST /mcp')
        self.assertEqual(_card_endpoint({'endpoint': '//other.example/mcp'}, base), '//other.example/mcp')

    def test_fuchss_tunnel_error_remains_error(self):
        import httpx
        from directory import crawlers
        transport = httpx.MockTransport(lambda req: httpx.Response(530, text='error code: 1033'))
        client = httpx.Client(transport=transport)
        with patch.object(crawlers.httpx, 'Client', return_value=client), patch.object(crawlers.time, 'sleep') as wait:
            with self.assertRaisesRegex(RuntimeError, '530'):
                crawlers.fetch_x402_fuchss()
        wait.assert_not_called()