"""Real app import/routes with disposable SQLite and synthetic provider replies.

No listener, real credentials, external connection, financial call or live DB.
Run as a separate process so application module globals remain isolated.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
import inspect
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


@contextmanager
def local_loop():
    # Windows builds its private asyncio self-pipe with a loopback socketpair.
    # Create/close that runtime resource outside the application's network guard.
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def run():
    with local_loop() as loop, tempfile.TemporaryDirectory(prefix='callmeie-admin-release-') as folder:
        root = Path(folder).resolve()
        env = {name: os.environ[name] for name in ('SYSTEMROOT', 'PATH', 'TEMP', 'TMP') if name in os.environ}
        env.update(ADMIN_TOKEN='synthetic-admin', VAPI_API_KEY='synthetic-provider',
                   VAPI_CALL_REPORT_SECRET='synthetic-call-report-only-secret-20261009',
                   DB_PATH=str(root/'app.sqlite'), AGENCY_DB_PATH=str(root/'billing.sqlite'))
        connect = sqlite3.connect
        def private_connect(path, *args, **kwargs):
            if Path(path).resolve().parent != root:
                raise AssertionError('database outside disposable fixture')
            return connect(path, *args, **kwargs)
        with patch.dict(os.environ, env, clear=True), patch.object(sqlite3, 'connect', side_effect=private_connect), \
             patch.object(socket.socket, 'connect', side_effect=AssertionError('external connection forbidden')):
            server = importlib.import_module('server')
            assert server.DB_PATH == env['DB_PATH'] and not server._USE_PG
            with server.get_db() as db:
                db.execute('INSERT INTO client_tokens(token,tenant_slug,tenant_display_name,assistant_ids) VALUES(?,?,?,?)',
                           ('synthetic-client','fixture-tenant','Fixture tenant','fixture-assistant'))
                db.execute('INSERT INTO client_tokens(token,tenant_slug,tenant_display_name,assistant_ids,revoked_at) VALUES(?,?,?,?,?)',
                           ('synthetic-revoked','other-tenant','Revoked tenant','fixture-assistant','2026-01-01'))
                db.execute('INSERT INTO client_tokens(token,tenant_slug,tenant_display_name,assistant_ids) VALUES(?,?,?,?)',
                           ('synthetic-other','other-tenant','Other tenant','other-assistant'))
                for call_id,assistant in [('shared-fixture','fixture-assistant'),('shared-fixture','other-assistant'),('private-fixture','fixture-assistant')]:
                    db.execute('INSERT INTO call_events(call_id,event_type,assistant,summary,detail) VALUES(?,?,?,?,?)',
                               (call_id,'call-ended',assistant,'Synthetic call','{}'))
                for tenant,note,actor in [('fixture-tenant','own-note','client'),('other-tenant','other-note','client'),(None,'owner-note','admin')]:
                    db.execute('INSERT INTO call_notes(call_id,tenant_slug,note,actor) VALUES(?,?,?,?)',
                               ('shared-fixture',tenant,note,actor))

            observed = []
            mode = {'value':'ok'}
            class Provider:
                def __init__(self, **kwargs): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                async def get(self, url, **kwargs):
                    assert url in ('https://api.vapi.ai/call','https://api.vapi.ai/assistant')
                    observed.append(url)
                    if mode['value']=='timeout':
                        raise httpx.ReadTimeout('synthetic provider timeout')
                    if mode['value']=='malformed':
                        return httpx.Response(200,json={'invalid':'synthetic'})
                    if url.endswith('/assistant'):
                        return httpx.Response(200,json=[{'id':'fixture-assistant','name':'Fixture assistant','model':{},'voice':{}}])
                    now = datetime.now(timezone.utc)
                    return httpx.Response(200,json=[{'id':'fixture-call','assistantId':'fixture-assistant',
                          'phoneNumberId':'fixture-line','status':'ended',
                          'startedAt':(now-timedelta(seconds=120)).isoformat(),
                          'endedAt':(now-timedelta(seconds=60)).isoformat()}])
            async def stripe(*args):
                return {'data':[]}
            real_client = httpx.AsyncClient
            server.httpx = SimpleNamespace(AsyncClient=Provider, TimeoutException=httpx.TimeoutException, RequestError=httpx.RequestError)
            server._stripe_get = stripe
            async def checks():
                transport = httpx.ASGITransport(app=server.app,raise_app_exceptions=True)
                async with real_client(transport=transport,base_url='http://fixture.test') as client:
                    # Actual route registration enforces credentials before JSON
                    # parsing. Status fixtures use only the disposable database.
                    report_headers={'Authorization':'Bearer '+env['VAPI_CALL_REPORT_SECRET']}
                    assert (await client.post('/vapi/call-ended',content='not-json')).status_code==401
                    assert (await client.post('/vapi/call-ended',headers={'Authorization':'Bearer synthetic-wrong'},content='not-json')).status_code==401
                    ignored=await client.post('/vapi/call-ended',headers=report_headers,json={'message':{'type':'status-update','status':'in-progress'}})
                    assert ignored.status_code==400
                    status_body={'message':{'type':'status-update','status':'in-progress',
                                 'timestamp':1760000400123,'call':{'id':'status-fixture',
                                 'assistantId':'fixture-assistant','phoneNumberId':'fixture-line',
                                 'customer':{'number':'must-not-store'}},
                                 'artifact':{'transcript':'must-not-store'}}}
                    stored=await client.post('/vapi/call-ended',headers=report_headers,json=status_body)
                    assert stored.status_code==200 and stored.json()['stored'] is True
                    duplicate=await client.post('/vapi/call-ended',headers=report_headers,json=status_body)
                    assert duplicate.status_code==200 and duplicate.json()['duplicate'] is True
                    with server.get_db() as db:
                        status_rows=db.execute("SELECT detail FROM call_events WHERE event_type='call-status'").fetchall()
                    assert len(status_rows)==1 and 'must-not-store' not in status_rows[0]['detail']
                    with patch.object(server,'store_status_event',side_effect=RuntimeError('synthetic storage failure')):
                        failed=await client.post('/vapi/call-ended',headers=report_headers,json=status_body)
                    assert failed.status_code==503 and failed.json()=={'error':'status_storage_unavailable'}
                    assert (await client.post('/vapi/call-ended',headers=report_headers,json=[])).status_code==400
                    assert (await client.post('/vapi/call-ended',headers=report_headers,content='not-json')).status_code==400
                    empty_final=await client.post('/vapi/call-ended',headers=report_headers,json={'message':{'type':'end-of-call-report'}})
                    assert empty_final.status_code==200 and empty_final.json()['skipped']=='no_call_id_or_assistant'
                    assert observed==[], 'call report boundary contacted provider'
                    for endpoint in ('/admin/api/operations-summary','/admin/api/flow-graph'):
                        response = await client.get(endpoint)
                        assert response.status_code==401,(endpoint,response.status_code)
                        assert response.headers.get('cache-control')=='private, no-store'
                        response = await client.get(endpoint,params={'token':'wrong-synthetic'})
                        assert response.status_code==401
                    assert observed==[], 'provider contacted before authorization'
                    header_response=await client.get('/admin/api/operations-summary',headers={'Authorization':'Bearer synthetic-admin'})
                    assert header_response.status_code==200
                    assert header_response.headers.get('cache-control')=='private, no-store'
                    assert 'token=' not in str(header_response.request.url)
                    assert server._admin_bearer.get() is None
                    async def empty_assistant(*args): return {}
                    # Small normal forms only: parser compatibility, not attack traffic.
                    no_file = await client.post('/api/docops/extract', data={'description':'fixture'})
                    assert no_file.status_code == 400 and no_file.json()['error'] == 'no_file'
                    text_file = await client.post('/api/docops/extract', files={'file':('fixture.txt',b'fixture','text/plain')})
                    assert text_file.status_code == 415 and text_file.json()['error'] == 'pdf_only'
                    empty_file = await client.post('/api/docops/extract', files={'file':('fixture.pdf',b'','application/pdf')})
                    assert empty_file.status_code == 400 and empty_file.json()['error'] == 'empty'
                    async def explicit_assistant(*args): return {'artifactPlan':{'recordingEnabled':False}}
                    for reader,expected in ((empty_assistant,'unverified'),(explicit_assistant,'verified')):
                        with patch.object(server,'_fetch_client_assistant',side_effect=reader):
                            for endpoint in ('/client/api/me','/client/api/settings'):
                                settings_response=await client.get(endpoint,headers={'Authorization':'Bearer synthetic-client'})
                                assert settings_response.status_code==200
                                assert settings_response.json()['recording_configuration_status']==expected
                                assert settings_response.json()['retention_enforcement_status']=='unverified'
                    for path in ('/admin/api/caller/{call_id}','/client/api/calls/{call_id}'):
                        route=next(r for r in server.app.routes if getattr(r,'path',None)==path)
                        assert not inspect.iscoroutinefunction(route.endpoint), 'blocking detail body must run in framework threadpool'
                    entered,released=threading.Event(),threading.Event()
                    def held_storage_read(*args,**kwargs):
                        entered.set()
                        if not released.wait(5): raise AssertionError('fixture release timed out')
                        return None
                    with patch.object(server,'_hetzner_presigned_for_call',side_effect=held_storage_read):
                        detail_task=asyncio.create_task(client.get('/client/api/calls/shared-fixture',headers={'Authorization':'Bearer synthetic-client'}))
                        try:
                            deadline=asyncio.get_running_loop().time()+2
                            while not entered.is_set() and asyncio.get_running_loop().time()<deadline:
                                await asyncio.sleep(0.005)
                            assert entered.is_set(), 'detail worker did not enter controlled storage read'
                            concurrent_health=await asyncio.wait_for(client.get('/health'),timeout=1)
                            assert concurrent_health.status_code==200 and not detail_task.done()
                        finally:
                            released.set()
                            finished=await asyncio.wait_for(detail_task,timeout=3)
                        assert finished.status_code==200 and finished.json()['notes'][0]['note']=='own-note'
                    for headers,params in (({'Authorization':'Bearer wrong'}, {'token':'synthetic-admin'}),
                                           ({'Authorization':'Bearer synthetic-admin'}, {'token':'wrong'}),
                                           ({'Authorization':'Basic synthetic'}, {'token':'synthetic-admin'})):
                        denied=await client.get('/admin/api/operations-summary',headers=headers,params=params)
                        assert denied.status_code==401
                    duplicate=await client.get('/admin/api/operations-summary',headers=[('Authorization','Bearer synthetic-admin'),('Authorization','Bearer synthetic-admin')])
                    assert duplicate.status_code==401
                    matched=await client.get('/admin/api/operations-summary',headers={'Authorization':'Bearer synthetic-admin'},params={'token':'synthetic-admin'})
                    assert matched.status_code==200
                    parallel=await asyncio.gather(client.get('/admin/api/operations-summary',headers={'Authorization':'Bearer synthetic-admin'}),client.get('/admin/api/operations-summary'))
                    assert [r.status_code for r in parallel]==[200,401]
                    assert server._admin_bearer.get() is None
                    observed.clear()
                    response = await client.get('/admin/api/operations-summary',params={'token':'synthetic-admin'})
                    assert response.status_code==200
                    payload=response.json()
                    assert payload['status_feed']['status']=='observed'
                    assert payload['status_feed']['coverage_verified'] is False
                    assert isinstance(payload['status_feed']['last_received_at'],(int,float))
                    line=payload['usage']['lines'][0]
                    assert line['configured_tenant_name']=='Fixture tenant'
                    assert line['allocation_status']=='configured_match'
                    assert line['tenant_id'] is None and line['billable_minutes'] is None
                    assert line['completed_provider_minutes']==1
                    assert payload['money']['status']=='partial' and payload['money']['profit_verified'] is False
                    assert payload['money']['payment_fees_basis']=='estimated_percentage_plus_fixed_fee'
                    assert observed==['https://api.vapi.ai/call']
                    assert 'synthetic-client' not in response.text and 'synthetic-revoked' not in response.text
                    mode['value']='timeout'
                    response = await client.get('/admin/api/operations-summary',params={'token':'synthetic-admin'})
                    assert response.status_code==200 and response.json()['usage']['status']=='unavailable'
                    assert response.json()['money']['status']=='unavailable'
                    response = await client.get('/admin/api/flow-graph',params={'token':'synthetic-admin'})
                    assert response.status_code==504
                    assert 'synthetic provider timeout' not in response.text
                    mode['value']='malformed'
                    response = await client.get('/admin/api/flow-graph',params={'token':'synthetic-admin'})
                    assert response.status_code==502
                    mode['value']='ok'
                    response = await client.get('/admin/api/flow-graph',params={'token':'synthetic-admin'})
                    assert response.status_code==200
                    assert set(response.json())=={'nodes','edges'}
                    health = await client.get('/health')
                    assert health.status_code==200
                    today = await client.get('/admin/api/today-actions',params={'token':'synthetic-admin'})
                    assert today.status_code==200
                    assert today.json()['coverage']=='partial'
                    assert today.json()['source_status']['sms_capability']=='failed'
                    assert today.json()['complete_period'] is False
                    for credential in ('synthetic-client','synthetic-other'):
                        header={'Authorization':'Bearer '+credential}
                        detail=await client.get('/client/api/calls/shared-fixture',headers=header)
                        assert detail.status_code==200
                        assert detail.headers.get('cache-control')=='private, no-store'
                        expected='own-note' if credential=='synthetic-client' else 'other-note'
                        assert [n['note'] for n in detail.json()['notes']]==[expected]
                        legacy=await client.get('/client/api/calls/shared-fixture',params={'token':credential})
                        assert legacy.status_code==200 and legacy.json()['notes']==detail.json()['notes']
                    admin_detail=await client.get('/admin/api/caller/shared-fixture',headers={'Authorization':'Bearer synthetic-admin'})
                    assert admin_detail.status_code==200 and len(admin_detail.json()['notes'])==3
                    wrong_tenant=await client.get('/client/api/calls/private-fixture',headers={'Authorization':'Bearer synthetic-other'})
                    assert wrong_tenant.status_code==403
                    for credential in ('synthetic-revoked','wrong','synthetic-admin'):
                        denied=await client.get('/client/api/calls',headers={'Authorization':'Bearer '+credential})
                        assert denied.status_code==401
                    assert (await client.get('/admin/api/operations-summary',headers={'Authorization':'Bearer synthetic-client'})).status_code==401
                    assert (await client.get('/client/api/today',params={'assistant':'fixture-assistant'},headers={'Authorization':'Bearer synthetic-client'})).status_code==401
                    for headers,params in [({'Authorization':'Bearer wrong'},{'token':'synthetic-client'}),
                                           ({'Authorization':'Bearer synthetic-client'},{'token':'synthetic-other'}),
                                           ({'Authorization':'Basic synthetic'},{'token':'synthetic-client'}),
                                           ([('Authorization','Bearer synthetic-client'),('Authorization','Bearer synthetic-client')],{})]:
                        assert (await client.get('/client/api/calls',headers=headers,params=params)).status_code==401
                    parallel_clients=await asyncio.gather(client.get('/client/api/calls',headers={'Authorization':'Bearer synthetic-client'}),
                                                          client.get('/client/api/calls',headers={'Authorization':'Bearer synthetic-other'}),client.get('/client/api/calls'))
                    assert [r.status_code for r in parallel_clients]==[200,200,401]
                    assert [r.json()['tenant_slug'] for r in parallel_clients[:2]]==['fixture-tenant','other-tenant']
                    assert server._admin_bearer.get() is None
                return {'scope':'real imported app, ASGI transport, disposable SQLite, mocked providers',
                        'status':'passed','authorized_usage':True,'auth_before_provider':True,
                        'revoked_mapping_excluded':True,'usage_provider_failure_explicit':True,
                        'flow_timeout_504':True,'flow_malformed_502':True,'flow_success_contract':True,
                        'today_partial_sources_explicit':True,
                        'bearer_and_legacy_auth_compatible':True,'credential_conflicts_rejected':True,'request_auth_isolated':True,
                        'client_bearer_legacy_revocation_scope_pass':True,'tenant_notes_isolated_admin_view_preserved':True,
                        'blocking_detail_does_not_block_health':True,
                        'protected_json_success_and_denial_no_store':True,
                        'client_config_targets_separate_from_enforcement':True,
                        'normal_small_form_parser_compatible':True,
                        'production_actions':False}
            result = loop.run_until_complete(checks())
            import json
            print(json.dumps(result))


if __name__=='__main__': run()
