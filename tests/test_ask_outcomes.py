"""Exercise actual Ask branch logic with fixture candidates and fake transport."""
import ast
import asyncio
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import time

ROOT = Path(__file__).resolve().parents[1]


class AskOutcomeTests(unittest.TestCase):
    def setUp(self):
        node = next(n for n in ast.parse((ROOT/'directory/ask.py').read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'answer_query')
        self.conn = Mock()
        self.llm = AsyncMock()
        self.log = Mock()
        self.ns = dict(db=SimpleNamespace(DEFAULT_DB_PATH='fixture', connect=Mock(return_value=self.conn)),
                       closing=closing, Any=object, cards=SimpleNamespace(build_service_card=lambda r:r),
                       _retrieve_rows=Mock(return_value=[{'slug':'fixture'}]),
                       _call_llm=self.llm, _prompt=lambda *args:'fixture', log=self.log, time=time,
                       _fallback=lambda q,c,l,r:{'llm_used':False,'fallback_reason':r},
                       _sanitize_llm_result=lambda *args:{'llm_used':True,'recommendations':[{'slug':'fixture'}]})
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-answer-query>','exec'),self.ns)

    def test_success_and_connection_closed_before_llm(self):
        async def respond(_):
            self.conn.close.assert_called_once()
            return {'recommendations':[{'idx':0}]}
        self.llm.side_effect=respond
        out=asyncio.run(self.ns['answer_query']('weather'))
        self.assertTrue(out['llm_used']);self.log.info.assert_called_once()

    def test_unavailable_bypass_and_no_candidates(self):
        self.llm.return_value=None
        out=asyncio.run(self.ns['answer_query']('weather'))
        self.assertEqual(out['fallback_reason'],'llm_unavailable');self.log.warning.assert_called_once()
        self.llm.reset_mock()
        out=asyncio.run(self.ns['answer_query']('weather',use_llm=False))
        self.assertEqual(out['fallback_reason'],'use_llm=false');self.llm.assert_not_called()
        self.ns['_retrieve_rows'].return_value=[]
        out=asyncio.run(self.ns['answer_query']('weather'))
        self.assertEqual(out['candidate_count'],0);self.llm.assert_not_called()

    def test_invalid_recommendation_falls_back(self):
        self.llm.return_value={'recommendations':[]}
        self.ns['_sanitize_llm_result']=lambda *args:{'recommendations':[]}
        out=asyncio.run(self.ns['answer_query']('weather'))
        self.assertEqual(out['fallback_reason'],'llm_returned_no_valid_recommendations')
        self.log.warning.assert_called_once()

    def test_prompt_caps_untrusted_metadata_without_mutating_cards(self):
        import json
        node = next(n for n in ast.parse((ROOT/'directory/ask.py').read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == '_prompt')
        ns = {'Any': object, 'json': json}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-prompt>', 'exec'), ns)
        card = {'slug': 'fixture', 'name': 'n'*2000, 'description': 'd'*100000,
                'call': {'resource_samples': [{'schema': 's'*100000}]}}
        prompt = ns['_prompt']('weather', [card]*30, 2)
        self.assertLess(len(prompt), 14000)
        self.assertNotIn('schema', prompt)
        self.assertEqual(len(card['description']), 100000)


if __name__ == '__main__': unittest.main()