"""Pure handler checks; no full application import, credentials or network."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'server.py'
handler = next(n for n in ast.parse(SOURCE.read_text(encoding='utf-8')).body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == 'admin_flow_graph')
handler.decorator_list = []
CODE = compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])), str(SOURCE), 'exec')

class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
class Timeout(Exception):
    pass
class RequestError(Exception):
    pass

def invoke(payload=None, status=200, error=None, bad_json=False):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs):
            if error: raise error
            def parse():
                if bad_json: raise ValueError('private synthetic response')
                return payload
            return SimpleNamespace(status_code=status, json=parse)
    ns = {'Query': lambda v: v, 'check_admin': lambda v: None,
          'os': SimpleNamespace(environ={'VAPI_API_KEY': 'synthetic'}),
          'httpx': SimpleNamespace(AsyncClient=Client, TimeoutException=Timeout, RequestError=RequestError),
          'HTTPException': HttpError, 'JSONResponse': lambda v: v}
    exec(CODE, ns)
    return asyncio.run(ns['admin_flow_graph']('synthetic'))

class FlowTests(unittest.TestCase):
    def expect(self, expected_status, **kwargs):
        with self.assertRaises(HttpError) as error: invoke(**kwargs)
        self.assertEqual(error.exception.status_code, expected_status)
        self.assertNotIn('private synthetic', error.exception.detail)
    def test_empty(self): self.assertEqual(invoke([]), {'nodes': [], 'edges': []})
    def test_transfer(self):
        result=invoke([{'id':'a','model':{'tools':[{'type':'transferCall','destinations':[{'number':'synthetic'}]}]}}])
        self.assertEqual(result['edges'][0]['to_target'], 'synthetic')
    def test_timeout(self): self.expect(504,error=Timeout('private synthetic'))
    def test_transport(self): self.expect(502,error=RequestError('private synthetic'))
    def test_provider_status(self): self.expect(502,status=503)
    def test_json(self): self.expect(502,bad_json=True)
    def test_wrong_root(self): self.expect(502,payload={'error':'synthetic'})
    def test_wrong_assistant(self): self.expect(502,payload=[None])
    def test_wrong_model(self): self.expect(502,payload=[{'model':'invalid'}])
    def test_wrong_voice(self): self.expect(502,payload=[{'voice':{'voiceId':42}}])
    def test_wrong_tool(self): self.expect(502,payload=[{'model':{'tools':[None]}}])
    def test_wrong_destinations(self): self.expect(502,payload=[{'model':{'tools':[{'type':'transferCall','destinations':{}}]}}])
    def test_wrong_transfer_plan(self): self.expect(502,payload=[{'model':{'tools':[{'type':'transferCall','destinations':[{'transferPlan':'bad'}]}]}}])
    def test_optional_nulls(self): self.assertEqual(len(invoke([{'id':'a','voice':None,'model':None}])['nodes']),1)

if __name__ == '__main__': unittest.main()
