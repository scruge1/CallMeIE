"""Actual Today handler with injected reads; no credentials, SDK or database."""
import ast
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'server.py'
NODE=next(n for n in ast.parse(SOURCE.read_text(encoding='utf-8')).body
          if isinstance(n,ast.AsyncFunctionDef) and n.name=='admin_today_actions')
NODE.decorator_list=[]
CODE=compile(ast.fix_missing_locations(ast.Module(body=[NODE],type_ignores=[])),str(SOURCE),'exec')

def invoke(*, fail='', demo=False, stripe_status=None, sms='ok', flagged=False):
    calls=[]
    row={'call_id':'fixture-call','created_at':datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
         'assistant':'fixture-assistant','summary':'Synthetic event','detail':json.dumps({'interest_level':'curious'})}
    class Connection:
        def __enter__(self):
            if fail=='database': raise RuntimeError('private fixture database detail')
            return self
        def __exit__(self,*args): pass
        def execute(self,sql,*args):
            calls.append('sql')
            if fail=='events' and "event_type = 'demo-complete'" in sql: raise RuntimeError('private fixture detail')
            if fail=='submissions' and 'FROM submissions' in sql: raise RuntimeError('private fixture detail')
            rows=[row] if demo and "event_type = 'demo-complete'" in sql else []
            return SimpleNamespace(fetchall=lambda:rows,fetchone=lambda:None)
    flag_reads=[]
    def flags(connection,ids=None):
        flag_reads.append(ids)
        if fail=='classifications' and len(flag_reads)>1: raise RuntimeError('private fixture detail')
        return {'fixture-call':'test'} if flagged else {}
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,*args,**kwargs):
            calls.append('provider-read')
            return SimpleNamespace(status_code=stripe_status,json=lambda:{'data':[]})
    async def probe(): return {'status':sms,'detail':'synthetic'}
    ns={'Query':lambda x:x,'check_admin':lambda x:calls.append('auth'),'get_db':Connection,
        '_get_call_classifications':flags,'OWL_STRIPE_API_KEY':'synthetic' if stripe_status else '',
        'httpx':SimpleNamespace(AsyncClient=Client),'_probe_twilio_from_sms':probe,
        'JSONResponse':lambda x:x,'json':json,'sys':sys}
    exec(CODE,ns)
    return asyncio.run(ns['admin_today_actions']('synthetic')),calls

class CoverageTests(unittest.TestCase):
    def test_healthy_empty_window_is_bounded_not_complete_period(self):
        result,calls=invoke()
        self.assertEqual(result['coverage'],'bounded')
        self.assertEqual(result['actions'],[])
        self.assertFalse(result['complete_period'])
        self.assertEqual(result['source_status']['stripe_sessions'],'not_configured')
        self.assertEqual(calls[0],'auth')
    def test_database_failure_is_partial_not_successful_zero(self):
        result,_=invoke(fail='database')
        self.assertEqual(result['coverage'],'partial')
        self.assertEqual(result['source_status']['call_events'],'failed')
        self.assertNotIn('private fixture',str(result))
    def test_event_failure_identified_without_hiding_other_status(self):
        result,_=invoke(fail='events')
        self.assertEqual(result['source_status']['call_events'],'failed')
        self.assertEqual(result['source_status']['submissions'],'ok')
        self.assertEqual(result['coverage'],'partial')
    def test_submission_failure_keeps_available_actions(self):
        result,_=invoke(fail='submissions',demo=True)
        self.assertEqual(result['coverage'],'partial')
        self.assertEqual(len(result['actions']),1)
        self.assertEqual(result['source_status']['submissions'],'failed')
    def test_configured_stripe_http_failure_is_not_unconfigured(self):
        result,_=invoke(stripe_status=503)
        self.assertEqual(result['source_status']['stripe_sessions'],'failed')
        self.assertEqual(result['coverage'],'partial')
    def test_sms_failure_keeps_fault_action_and_partial_coverage(self):
        result,_=invoke(sms='fail')
        self.assertEqual(result['actions'][0]['state'],'system_fault')
        self.assertEqual(result['source_status']['sms_capability'],'failed')
        self.assertEqual(result['coverage'],'partial')
    def test_final_classification_failure_is_visible(self):
        result,_=invoke(fail='classifications',demo=True)
        self.assertEqual(result['source_status']['classifications'],'failed')
        self.assertEqual(result['coverage'],'partial')
    def test_existing_flagged_call_exclusion_retained(self):
        result,_=invoke(demo=True,flagged=True)
        self.assertEqual(result['actions'],[])
        self.assertEqual(result['coverage'],'bounded')

if __name__=='__main__': unittest.main()
