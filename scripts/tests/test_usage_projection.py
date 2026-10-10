"""Synthetic read-only usage checks. No app startup, database or provider calls."""
import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('usage', ROOT / 'billing' / 'usage.py')
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)
NOW = 1760000400
START = NOW - 3600

def iso(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

def call(**changes):
    return {'id': 'a', 'phoneNumberId': 'line-a', 'status': 'ended',
            'startedAt': iso(NOW - 120), 'endedAt': iso(NOW - 60), **changes}

def project(records, tenant_bindings=None, **changes):
    return usage.project_usage({'status': 'ok', 'observed_at': NOW,
                                'calls': records, **changes}, now=NOW, window_start=START, tenant_bindings=tenant_bindings,
                               terminal_observations={'status':'ok','call_ids':[]})

class ProjectionTests(unittest.TestCase):
    def test_completed_and_unknown_authority(self):
        result = project([call()])
        line = result['lines'][0]
        self.assertEqual(line['completed_provider_minutes'], 1)
        for field in ('tenant_id', 'hotel_id', 'confirmed_ai_minutes', 'billable_minutes', 'allowance_remaining_minutes'):
            self.assertIsNone(line[field])
        self.assertEqual(result['coverage'], 'partial')
        self.assertFalse(result['carryover_calls_included'])
    def test_active_snapshot_not_ai(self):
        line = project([call(status='in-progress', endedAt=None)])['lines'][0]
        self.assertEqual(line['observed_active_calls'], 1)
        self.assertEqual(line['active_provider_minutes_estimate'], 2)
        self.assertIsNone(line['confirmed_ai_minutes'])
    def test_stale_has_no_live_estimate(self):
        result = project([call(status='in-progress', endedAt=None)], observed_at=NOW - 91)
        self.assertEqual(result['status'], 'stale')
        self.assertIsNone(result['lines'][0]['active_provider_minutes_estimate'])
    def test_future_snapshot_is_not_fresh(self):
        self.assertEqual(project([], observed_at=NOW + 1)['status'], 'stale')
    def test_same_duplicate_not_counted_twice(self):
        self.assertEqual(project([call(), call()])['lines'][0]['completed_calls'], 1)
    def test_conflicts_excluded_regardless_of_order(self):
        a, b = call(), call(endedAt=iso(NOW - 30))
        for records in ([a,b,a], [b,a,b]):
            result = project(records)
            self.assertEqual(result['conflicting_calls'], 1)
            self.assertEqual(result['lines'], [])
    def test_separate_lines_and_unallocated(self):
        result = project([call(), call(id='b', phoneNumberId='line-b'), call(id='c', phoneNumberId=None)])
        self.assertEqual(len(result['lines']), 3)
        self.assertIsNone(result['lines'][0]['line_id'])
    def test_duration_is_clipped_to_period(self):
        line = project([call(startedAt=iso(START - 60), endedAt=iso(START + 60))])['lines'][0]
        self.assertEqual(line['completed_provider_minutes'], 1)
    def test_call_ended_before_window_is_not_a_today_call(self):
        result = project([call(startedAt=iso(START-120), endedAt=iso(START-60))])
        self.assertEqual(result['lines'], [])
        self.assertEqual(result['outside_window_calls'], 1)
    def test_call_ending_at_window_boundary_has_no_today_usage(self):
        self.assertEqual(project([call(startedAt=iso(START-60), endedAt=iso(START))])['lines'], [])
    def test_update_query_scope_does_not_claim_complete_carryover(self):
        result = project([call(startedAt=iso(START-60), endedAt=iso(START+60))],
                         query_scope='calls_updated_since_utc_midnight')
        self.assertEqual(result['query_scope'], 'calls_updated_since_utc_midnight')
        self.assertTrue(result['carryover_calls_included'])
        self.assertEqual(result['coverage'], 'partial')
        self.assertIsNone(result['billable_minutes'])
    def test_bad_timings_not_zero_confirmed_calls(self):
        for changes in ({'startedAt':'bad'}, {'endedAt':iso(NOW+10)},
                        {'endedAt':iso(NOW-180)}, {'endedAt':'bad'},
                        {'startedAt':'2025-01-01T00:00:00'}, {'status':'ended', 'endedAt':None}):
            line = project([call(**changes)])['lines'][0]
            self.assertEqual(line['unknown_calls'], 1)
            self.assertEqual(line['completed_calls'], 0)
    def test_bad_records_and_cap(self):
        self.assertEqual(project([None, {}, call(id=123)])['rejected_records'], 3)
        self.assertTrue(project([call(id=str(i)) for i in range(100)])['limit_reached'])
    def test_unavailable_different_from_empty(self):
        self.assertEqual(project([], status='unavailable')['status'], 'unavailable')
        self.assertEqual(project([])['status'], 'fresh')
    def test_private_fields_not_projected(self):
        result = project([call(customer={'number':'private-number'}, transcript='private-body', recordingUrl='private-url')])
        self.assertNotIn('private-', str(result))

class TerminalProjectionTests(unittest.TestCase):
    def result(self, records, terminal):
        return usage.project_usage({'status':'ok','observed_at':NOW,'calls':records},
                                   now=NOW, window_start=START, terminal_observations=terminal)

    def test_committed_end_prevents_active_snapshot_resurrection(self):
        line=self.result([call(status='in-progress',endedAt=None)], {'status':'ok','call_ids':['a']})['lines'][0]
        self.assertEqual(line['observed_active_calls'],0)
        self.assertEqual(line['active_provider_minutes_estimate'],0)
        self.assertEqual(line['ended_without_timing_calls'],1)
        self.assertEqual(line['unknown_calls'],1)
        self.assertEqual(line['completed_provider_minutes'],0)
        self.assertIsNone(line['billable_minutes'])

    def test_missing_or_failed_terminal_read_withholds_active_estimate(self):
        for terminal in (None, {}, [], 'bad', {'status':'unavailable','call_ids':[]}, {'status':'ok','call_ids':None}, {'status':'ok','call_ids':[None]}):
            with self.subTest(terminal=terminal):
                result=self.result([call(status='in-progress',endedAt=None)],terminal)
                line=result['lines'][0]
                self.assertEqual(result['terminal_guard_status'],'unavailable')
                self.assertEqual(line['active_state_status'],'unavailable')
                self.assertIsNone(line['active_provider_minutes_estimate'])
                self.assertEqual(line['unknown_calls'],1)

    def test_terminal_read_failure_does_not_remove_completed_usage(self):
        line=self.result([call()], {'status':'unavailable','call_ids':[]})['lines'][0]
        self.assertEqual(line['completed_calls'],1)
        self.assertEqual(line['completed_provider_minutes'],1)

    def test_completed_duration_still_uses_provider_times(self):
        line=self.result([call()], {'status':'ok','call_ids':['a']})['lines'][0]
        self.assertEqual(line['completed_provider_minutes'],1)
        self.assertEqual(line['ended_without_timing_calls'],0)

    def test_terminal_marker_does_not_affect_another_call_on_same_line(self):
        line=self.result([call(status='in-progress',endedAt=None)], {'status':'ok','call_ids':['other']})['lines'][0]
        self.assertEqual(line['observed_active_calls'],1)
        self.assertEqual(line['active_provider_minutes_estimate'],2)


class TenantProjectionTests(unittest.TestCase):
    def bindings(self, *rows): return {'status':'ok', 'rows':list(rows)}
    def row(self, tenant='tenant-a', name='Configured A', ids=('assistant-a',)):
        return {'tenant_slug':tenant, 'display_name':name, 'assistant_ids':ids}
    def test_configured_match_is_not_authoritative_tenant(self):
        result = project([call(assistantId='assistant-a')], tenant_bindings=self.bindings(self.row()))
        line=result['lines'][0]
        self.assertEqual(line['configured_tenant_name'], 'Configured A')
        self.assertEqual(line['allocation_status'], 'configured_match')
        self.assertIsNone(line['tenant_id'])
        self.assertIsNone(line['billable_minutes'])
    def test_multiple_tenants_sharing_assistant_held(self):
        rows=(self.row(), self.row('tenant-b','Configured B'))
        line=project([call(assistantId='assistant-a')],tenant_bindings=self.bindings(*rows))['lines'][0]
        self.assertEqual(line['allocation_status'],'conflicting')
        self.assertIsNone(line['configured_tenant_id'])
        self.assertEqual(line['completed_calls'],1)
    def test_multiple_tokens_same_tenant_do_not_conflict(self):
        line=project([call(assistantId='assistant-a')],tenant_bindings=self.bindings(self.row(),self.row()))['lines'][0]
        self.assertEqual(line['allocation_status'],'configured_match')
    def test_shared_line_does_not_mix_tenants(self):
        rows=(self.row(),self.row('tenant-b','Configured B',('assistant-b',)))
        records=[call(assistantId='assistant-a'),call(id='b',assistantId='assistant-b')]
        result=project(records,tenant_bindings=self.bindings(*rows))
        self.assertEqual(len(result['lines']),2)
        self.assertEqual({line['configured_tenant_id'] for line in result['lines']},{'tenant-a','tenant-b'})
        self.assertEqual(sum(line['completed_calls'] for line in result['lines']),2)
    def test_changed_assistant_conflicts_same_call(self):
        result=project([call(assistantId='assistant-a'),call(assistantId='assistant-b')],tenant_bindings=self.bindings(self.row()))
        self.assertEqual(result['conflicting_calls'],1)
        self.assertEqual(result['lines'],[])
    def test_unmapped_and_unavailable_distinct(self):
        self.assertEqual(project([call()],tenant_bindings=self.bindings())['lines'][0]['allocation_status'],'unassigned')
        self.assertEqual(project([call()])['lines'][0]['allocation_status'],'configuration_unavailable')

def read_bindings(rows=(), error=False):
    nodes=[n for n in ast.parse((ROOT/'server.py').read_text(encoding='utf-8')).body
           if isinstance(n, ast.FunctionDef) and n.name in ('_usage_tenant_bindings','_client_assistant_ids')]
    queries=[]
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql):
            queries.append(sql)
            if error: raise RuntimeError('private-database-error')
            return SimpleNamespace(fetchall=lambda:rows)
    ns={'get_db':Connection}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<bindings>','exec'),ns)
    return ns['_usage_tenant_bindings'](),queries

class BindingReaderTests(unittest.TestCase):
    def test_existing_csv_and_pg_array_forms(self):
        for ids in ('a,b','{a,b}',['a','b']):
            result,queries=read_bindings([{'tenant_slug':'tenant-a','tenant_display_name':'A','assistant_ids':ids}])
            self.assertEqual(result['rows'][0]['assistant_ids'],['a','b'])
            self.assertIn('WHERE revoked_at IS NULL',queries[0])
            self.assertNotIn('SELECT token',queries[0])
    def test_tokens_never_projected(self):
        result,_=read_bindings([{'tenant_slug':'tenant-a','assistant_ids':['a'],'token':'private-token'}])
        self.assertNotIn('private-token',str(result))
    def test_bad_mapping_blocks_labels(self):
        for row in ({'tenant_slug':None,'assistant_ids':['a']},{'tenant_slug':'tenant-a','assistant_ids':{'a':True}}, {'tenant_slug':'tenant-a','assistant_ids':[4]}):
            self.assertEqual(read_bindings([row])[0],{'status':'unavailable','rows':[]})
    def test_failure_not_empty_configured_state(self):
        self.assertEqual(read_bindings(error=True)[0],{'status':'unavailable','rows':[]})
        self.assertEqual(read_bindings([])[0],{'status':'ok','rows':[]})
    def test_cap_cannot_hide_another_tenant(self):
        self.assertEqual(read_bindings([{}]*1001)[0],{'status':'unavailable','rows':[]})

def fetch(payload=None, status=200, error=False, configured=True):
    node = next(n for n in ast.parse((ROOT/'server.py').read_text(encoding='utf-8')).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == '_vapi_calls_window')
    requests = []
    class RequestError(Exception): pass
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            requests.append((url, kwargs['params']))
            if error: raise RequestError('private')
            return SimpleNamespace(status_code=status, json=lambda: payload)
    import time
    env = {'VAPI_API_KEY':'synthetic'} if configured else {}
    ns = {'os':SimpleNamespace(environ=env), '_time':time,
          'httpx':SimpleNamespace(AsyncClient=Client, RequestError=RequestError)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<snapshot>', 'exec'), ns)
    return asyncio.run(ns['_vapi_calls_window'](START)), requests

class FetchTests(unittest.TestCase):
    def test_missing_key_no_request(self):
        result, requests = fetch(configured=False)
        self.assertEqual(result['reason'], 'not_configured')
        self.assertEqual(requests, [])
    def test_provider_failure_not_empty_success(self):
        self.assertEqual(fetch(status=500)[0]['reason'], 'provider_error')
        self.assertEqual(fetch(error=True)[0]['reason'], 'provider_unreachable')
    def test_invalid_root_not_empty_success(self):
        self.assertEqual(fetch(payload={})[0]['reason'], 'invalid_response')
    def test_success_has_capture_time(self):
        result, requests = fetch(payload=[])
        self.assertEqual(result['status'], 'ok')
        self.assertIsInstance(result['observed_at'], int)
        self.assertEqual(requests[0][1]['limit'], 100)
    def test_one_update_time_query_includes_midnight_boundary(self):
        result, requests = fetch(payload=[])
        self.assertEqual(len(requests), 1)
        self.assertEqual(set(requests[0][1]), {'limit','updatedAtGe'})
        self.assertEqual(requests[0][1]['updatedAtGe'], iso(START).replace('+00:00','Z'))
        self.assertEqual(result['query_scope'], 'calls_updated_since_utc_midnight')

class IntegrationTests(unittest.TestCase):
    def test_operations_uses_one_snapshot_and_real_projection(self):
        import sys
        from unittest.mock import patch
        node = next(n for n in ast.parse((ROOT/'server.py').read_text(encoding='utf-8')).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'admin_operations_summary')
        node.decorator_list = []
        seen = []
        async def stripe(*args): return {'data':[]}
        async def snapshot(start):
            import time
            now = int(time.time())
            seen.append(start)
            return {'status':'ok', 'observed_at':now, 'calls':[call(startedAt=iso(now-120), endedAt=iso(now-60))]}
        def db(): raise RuntimeError('synthetic unavailable database')
        ns = {'Query':lambda value:value, 'check_admin':lambda token:seen.append('auth'),
              '_stripe_get':stripe, '_vapi_calls_window':snapshot, 'get_db':db,
              '_usage_tenant_bindings':lambda:{'status':'unavailable','rows':[]},
              'JSONResponse':lambda value:value, 'VAPI_RATE_PER_MIN_EUR':0.1,
              'TWILIO_SMS_INTL_RATE_EUR':0.1}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])), '<operations>', 'exec'), ns)
        with patch.dict(sys.modules, {'billing.usage':usage}):
            result = asyncio.run(ns['admin_operations_summary']('synthetic'))
        self.assertEqual(seen[0], 'auth')
        self.assertEqual(len(seen), 2)
        self.assertEqual(result['usage']['lines'][0]['completed_provider_minutes'], 1)
        self.assertEqual(result['today']['vapi_minutes'], 1)
        self.assertIsNone(result['usage']['billable_minutes'])

if __name__ == '__main__': unittest.main()
