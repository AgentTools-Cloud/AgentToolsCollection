"""No application startup, production DB, HTTP requests or mail side effects."""
import ast
from contextlib import closing
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parents[1]


class CategoryBrowseTests(unittest.TestCase):
    def setUp(self):
        # Execute actual category functions only: never import jobs/auth or
        # run startup migrations just to test a rendered page.
        tree = ast.parse((ROOT / 'directory/routes.py').read_text())
        selected = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ('_category_rows', 'categories_page'):
                selected.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in (
                    '_CATEGORY_PAGE_SIZE', '_CATEGORY_CACHE_TTL', '_category_cache_lock', '_CATEGORY_KINDS'
                ) for t in node.targets):
                selected.append(node)
        self.conn = Mock()
        self.open = Mock(return_value=self.conn)
        self.rows = [{'category': f'tag-{i:04d}', 'count': 1} for i in range(151)]
        self.db = SimpleNamespace(**{n: Mock(return_value=self.rows) for n in
                                     ('list_categories', 'mcp_categories', 'a2a_categories')})
        templates = Jinja2Templates(directory=str(ROOT / 'directory/templates'))
        templates.env.globals.update(build_version='test', current_user=lambda r: None)
        ns = dict(closing=closing, threading=threading, time=time, urlencode=urlencode,
                  Query=Query, Request=Request, HTMLResponse=HTMLResponse,
                  _category_cache={}, _conn=self.open, db=self.db, router=APIRouter(), TEMPLATES=templates)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT / 'directory/routes.py'), 'exec'), ns)
        self.ns = ns
        app = FastAPI(); app.include_router(ns['router']); self.client = TestClient(app)

    def test_bounded_pages_and_all_long_tail_accessible(self):
        bodies = [self.client.get(f'/categories?kind=a2a&page={p}').text for p in (1, 2, 3)]
        self.assertEqual([x.count('data-category-link') for x in bodies], [60, 60, 31])
        for i in range(151):
            self.assertEqual(sum(f'/a2a?q=tag-{i:04d}' in x for x in bodies), 1)
        self.db.a2a_categories.assert_called_once()
        self.conn.close.assert_called_once()

    def test_x402_correct_destination_and_encoding(self):
        self.db.list_categories.return_value = [{'category': 'a&b /中文', 'count': 3}]
        text = self.client.get('/categories').text
        self.assertIn('/x402?category=a%26b+%2F', text)
        self.assertNotIn('href="/?category=', text)

    def test_filter_preserved_and_no_query_cache_growth(self):
        text = self.client.get('/categories?kind=a2a&q=TAG-00&page=2').text
        self.assertIn('q=TAG-00&amp;page=1', text)
        self.assertEqual(text.count('data-category-link'), 40)
        for q in ('empty', 'tag-0149', 'tag-0000'):
            self.client.get('/categories', params={'kind': 'a2a', 'q': q})
        self.assertEqual(len(self.ns['_category_cache']), 1)
        self.db.a2a_categories.assert_called_once()

    def test_validation_empty_and_clamped_page(self):
        for suffix in ('kind=no', 'page=0', 'page=-1', 'page=100001', 'q='+'x'*201):
            self.assertEqual(self.client.get('/categories?'+suffix).status_code, 422)
        self.assertIn('No matching topics', self.client.get('/categories?q=absent').text)
        self.assertIn('Page 3 of 3', self.client.get('/categories?page=999').text)

    def test_cache_expiry_and_failure_closes(self):
        self.client.get('/categories')
        self.ns['_category_cache']['x402'] = (-1000, self.rows)
        self.client.get('/categories')
        self.assertEqual(self.db.list_categories.call_count, 2)
        self.db.mcp_categories.side_effect = ValueError('fixture')
        with self.assertRaises(ValueError): self.client.get('/categories?kind=mcp')
        self.assertEqual(self.conn.close.call_count, 3)

    def test_html_escape(self):
        self.db.list_categories.return_value = [{'category': '<script>alert(1)</script>', 'count': 1}]
        text = self.client.get('/categories').text
        self.assertNotIn('<script>alert(1)</script>', text)
        self.assertIn('&lt;script&gt;', text)


if __name__ == '__main__': unittest.main()