"""Real app import/routes with disposable SQLite and synthetic provider replies.

No listener, real credentials, external connection, financial call or live DB.
Run as a separate process so application module globals remain isolated.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
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
                    for endpoint in ('/admin/api/operations-summary','/admin/api/flow-graph'):
                        response = await client.get(endpoint)
                        assert response.status_code==401,(endpoint,response.status_code)
                        response = await client.get(endpoint,params={'token':'wrong-synthetic'})
                        assert response.status_code==401
                    assert observed==[], 'provider contacted before authorization'
                    response = await client.get('/admin/api/operations-summary',params={'token':'synthetic-admin'})
                    assert response.status_code==200
                    payload=response.json()
                    line=payload['usage']['lines'][0]
                    assert line['configured_tenant_name']=='Fixture tenant'
                    assert line['allocation_status']=='configured_match'
                    assert line['tenant_id'] is None and line['billable_minutes'] is None
                    assert line['completed_provider_minutes']==1
                    assert observed==['https://api.vapi.ai/call']
                    assert 'synthetic-client' not in response.text and 'synthetic-revoked' not in response.text
                    mode['value']='timeout'
                    response = await client.get('/admin/api/operations-summary',params={'token':'synthetic-admin'})
                    assert response.status_code==200 and response.json()['usage']['status']=='unavailable'
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
                return {'scope':'real imported app, ASGI transport, disposable SQLite, mocked providers',
                        'status':'passed','authorized_usage':True,'auth_before_provider':True,
                        'revoked_mapping_excluded':True,'usage_provider_failure_explicit':True,
                        'flow_timeout_504':True,'flow_malformed_502':True,'flow_success_contract':True,
                        'production_actions':False}
            result = loop.run_until_complete(checks())
            import json
            print(json.dumps(result))


if __name__=='__main__': run()
