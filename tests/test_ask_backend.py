"""Actual request builder with synthetic environment and HTTPX mock transport."""
import ast
import asyncio
import json
import logging
import os
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]


class AskBackendTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={'choices':[{'message':{'content':'{"recommendations":[{"idx":0}]}'}}]})
        transport = httpx.MockTransport(respond)
        fake = SimpleNamespace(AsyncClient=lambda **kw:httpx.AsyncClient(transport=transport, **kw), Timeout=httpx.Timeout)
        names = {'_call_llm','_extract_json','_env_float','_env_int','_allowed_model'}
        tree = ast.parse((ROOT/'directory/ask.py').read_text())
        nodes = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        self.ns = dict(os=os, re=re, json=json, Any=Any, httpx=fake, log=logging.getLogger('test.ask'))
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<actual-llm-builder>','exec'),self.ns)
        self.env = {'AGENT_TOOLS_ASK_BASE_URL':'https://ask.example','AGENT_TOOLS_ASK_MODEL':'ask-model',
                    'AGENT_TOOLS_ASK_API_KEY':'synthetic-ask','AGENT_TOOLS_SAFETY_BASE_URL':'https://safety.example',
                    'AGENT_TOOLS_SAFETY_MODEL':'safety-model','AGENT_TOOLS_SAFETY_API_KEY':'synthetic-safety'}

    def test_default_keeps_ask_credential(self):
        with patch.dict(os.environ,self.env,clear=True):
            self.assertTrue(asyncio.run(self.ns['_call_llm']('fixture')))
        self.assertEqual(str(self.requests[0].url),'https://ask.example/v1/chat/completions')
        self.assertEqual(self.requests[0].headers['Authorization'],'Bearer synthetic-ask')

    def test_explicit_shared_backend_and_no_silent_fallback(self):
        self.env['AGENT_TOOLS_ASK_USE_SAFETY_BACKEND']='1'
        with patch.dict(os.environ,self.env,clear=True):
            self.assertTrue(asyncio.run(self.ns['_call_llm']('fixture')))
        req=self.requests[0]
        self.assertEqual(str(req.url),'https://safety.example/v1/chat/completions')
        self.assertEqual(req.headers['Authorization'],'Bearer synthetic-safety')
        self.assertEqual(json.loads(req.content)['model'],'safety-model')
        self.requests.clear();self.env['AGENT_TOOLS_SAFETY_API_KEY']=''
        with patch.dict(os.environ,self.env,clear=True):
            self.assertIsNone(asyncio.run(self.ns['_call_llm']('fixture')))
        self.assertFalse(self.requests)


if __name__ == '__main__': unittest.main()