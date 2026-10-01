"""Persistence, owner-only access, provider boundaries and message workflow."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import personal_agent as pa


@pytest.fixture
def workspace(tmp_path):
    @contextmanager
    def db():
        conn = sqlite3.connect(tmp_path / 'calls.db')
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    with db() as conn:
        conn.executescript("CREATE TABLE call_events(id INTEGER PRIMARY KEY,created_at TEXT DEFAULT CURRENT_TIMESTAMP,call_id TEXT,event_type TEXT,assistant TEXT,summary TEXT,detail TEXT); CREATE TABLE call_notes(id INTEGER PRIMARY KEY,created_at TEXT DEFAULT CURRENT_TIMESTAMP,call_id TEXT,tenant_slug TEXT,note TEXT,actor TEXT);")
    def admin(token):
        if token != 'test-admin':
            raise HTTPException(401)
    app = FastAPI()
    pa.install(app, db, admin)
    return TestClient(app), db


def tool(db, kind='personal', urgency='normal', cid='synthetic-call'):
    return {'message': {'type':'tool-calls', 'call':{'id':cid,'assistantId':pa.ASSISTANT_ID}, 'toolCallList':[{'id':'tool-1','function':{'name':'savePersonalMessage','arguments':json.dumps({'caller_name':'Alex Test','callback_number':'0871234567','reason':'Dinner tomorrow','requested_action':'Call Alex','kind':kind,'urgency':urgency})}}]}}


def post_tool(client, db, **kw):
    return client.post('/vapi/personal-agent',json=tool(db,**kw),headers={'x-personal-agent-secret':pa.settings(db)['webhook_secret']})


def admin_url(path=''):
    return '/admin/api/personal-agent'+path+'?token=test-admin'


def test_admin_auth_and_secret_not_exposed(workspace):
    client, db = workspace
    assert client.get('/admin/api/personal-agent').status_code == 401
    r = client.get(admin_url())
    assert r.status_code == 200
    assert pa.settings(db)['webhook_secret'] not in r.text


def test_webhook_auth_and_wrong_assistant(workspace):
    client, db = workspace
    body=tool(db)
    assert client.post('/vapi/personal-agent',json=body).status_code == 401
    body['message']['call']['assistantId']='shared-demo'
    assert client.post('/vapi/personal-agent',json=body,headers={'x-personal-agent-secret':pa.settings(db)['webhook_secret']}).status_code == 403


def test_message_survives_call_and_repeated_tool(workspace):
    client, db=workspace
    assert post_tool(client,db).json()['results'][0]['result'].find('true') > 0
    assert post_tool(client,db).status_code == 200
    with db() as conn:
        assert conn.execute('SELECT count(*) n FROM call_events').fetchone()['n'] == 1
    calls=client.get(admin_url()).json()['calls']
    assert len(calls)==1 and calls[0]['intake']['reason']=='Dinner tomorrow'
    assert calls[0]['caller_phone']=='0871234567'


def test_note_and_followup_persist(workspace):
    client, db=workspace
    post_tool(client,db)
    assert client.post(admin_url('/calls/synthetic-call/notes'),json={'note':'Called Alex. Confirmed dinner.'}).json()['saved']
    assert client.patch(admin_url('/calls/synthetic-call'),json={'status':'waiting','next_action':'Confirm tomorrow','due_at':'2026-10-03T18:00:00+00:00'}).status_code==200
    out=client.get(admin_url('/calls/synthetic-call')).json()
    assert out['notes'][0]['note']=='Called Alex. Confirmed dinner.'
    assert out['state']['status']=='waiting'


def test_other_assistant_calls_not_read_or_changed(workspace):
    client,db=workspace
    with db() as conn:
        conn.execute("INSERT INTO call_events(call_id,event_type,assistant,summary,detail) VALUES ('other','call-ended','shared-demo','private','{}')")
    assert client.get(admin_url()).json()['calls']==[]
    for path,method,body in [('/calls/other','get',None),('/calls/other','patch',{'status':'closed'}),('/calls/other/notes','post',{'note':'No'})]:
        r=getattr(client,method)(admin_url(path),**({'json':body} if body else {}))
        assert r.status_code==404


def test_private_knowledge_never_sent_to_model():
    config=copy.deepcopy(pa.DEFAULT_CONFIG)
    config['private_notes']='PRIVATE HOME ADDRESS MUST NOT LEAK'
    patch=pa.assistant_patch(config,'secret-not-for-frontend')
    assert config['private_notes'] not in json.dumps(patch)
    assert [t['type'] for t in patch['model']['tools']]==['endCall','function']
    assert 'Never book appointments' in pa.compile_prompt(config)


def test_revision_conflict(workspace):
    client,db=workspace
    payload={'config':copy.deepcopy(pa.DEFAULT_CONFIG),'revision':1}
    assert client.put(admin_url('/config'),json=payload).status_code==200
    assert client.put(admin_url('/config'),json=payload).status_code==409


def test_invalid_notification_destination_rejected(workspace):
    client,db=workspace
    config=copy.deepcopy(pa.DEFAULT_CONFIG);config['notification_channel']='caller_destination'
    assert client.put(admin_url('/config'),json={'config':config,'revision':1}).status_code==422
    config=copy.deepcopy(pa.DEFAULT_CONFIG);config['send_to']='attacker'
    assert client.put(admin_url('/config'),json={'config':config,'revision':1}).status_code==422


def test_tool_invalid_or_oversized_data(workspace):
    client,db=workspace
    headers={'x-personal-agent-secret':pa.settings(db)['webhook_secret']}
    body=tool(db);body['message']['toolCallList'][0]['function']['arguments']='not-json'
    assert client.post('/vapi/personal-agent',json=body,headers=headers).status_code==422
    assert client.post('/vapi/personal-agent',content='x'*131073,headers=headers).status_code==413
    with pytest.raises(HTTPException):pa.clean_intake({'reason':'x'*2001})


def test_report_and_fallback_are_idempotent(workspace):
    client,db=workspace
    call={'id':'drop-test','assistantId':pa.ASSISTANT_ID,'customer':{'number':'+353871234567'},'artifact':{'transcript':'User: hello'},'status':'ended'}
    assert pa.persist_report(call,{},db)
    assert not pa.persist_report(call,{},db)
    out=client.get(admin_url('/calls/drop-test')).json()
    assert out['transcript']=='User: hello'
    assert 'did not finish' in out['state']['intake']['reason']


def test_sync_filters_provider_results(workspace,monkeypatch):
    client,db=workspace
    async def provider(path,method='GET',payload=None):
        return [{'id':'owned-ended','assistantId':pa.ASSISTANT_ID,'status':'ended','transcript':'User: test'}, {'id':'shared','assistantId':'demo','status':'ended'}, {'id':'live','assistantId':pa.ASSISTANT_ID,'status':'in-progress'}]
    monkeypatch.setattr(pa,'provider_call',provider)
    assert client.post(admin_url('/sync')).json()['imported']==1
    assert len(client.get(admin_url()).json()['calls'])==1


def test_publish_is_dedicated_and_verifies_readback(workspace,monkeypatch):
    client,db=workspace;seen=[]
    async def provider(path,method='GET',payload=None):
        seen.append((path,method,payload))
        return seen[0][2]
    monkeypatch.setattr(pa,'provider_call',provider)
    assert client.post(admin_url('/publish')).json()['published']
    assert all(x[0]=='assistant/'+pa.ASSISTANT_ID for x in seen)
    assert pa.settings(db)['published_revision']==1


def test_sms_labels_owner_only_and_no_duplicate_alert(workspace,monkeypatch):
    client,db=workspace;sent=[]
    monkeypatch.setenv('TWILIO_ACCOUNT_SID','test-account');monkeypatch.setenv('TWILIO_AUTH_TOKEN','test-auth')
    monkeypatch.setenv('OWNER_NOTIFICATION_NUMBER','+353850000000');monkeypatch.setenv('TWILIO_FROM_NUMBER','+16620000000')
    class FakeClient:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            sent.append(kwargs['data'])
            return httpx.Response(201,json={'sid':'SMtest','status':'queued'})
    monkeypatch.setattr(pa.httpx,'AsyncClient',FakeClient)
    with db() as conn:
        config=copy.deepcopy(pa.DEFAULT_CONFIG);config['notification_channel']='sms';config['notification_preview']='full'
        conn.execute('UPDATE personal_agent_settings SET config=?,published_config=?',(json.dumps(config),json.dumps(config)))
    assert post_tool(client,db,urgency='urgent').status_code==200
    assert post_tool(client,db,urgency='urgent').status_code==200
    assert len(sent)==1
    assert sent[0]['To']=='+353850000000'
    assert sent[0]['From']=='CALLMEIE'
    assert sent[0]['Body'].startswith('Personal agent | PERSONAL CALL | URGENT')
    assert 'Dinner tomorrow' in sent[0]['Body']
    assert 'token=' not in sent[0]['Body']


def test_spam_no_alert(workspace,monkeypatch):
    client,db=workspace
    with db() as conn:
        config=copy.deepcopy(pa.DEFAULT_CONFIG);config['notification_channel']='sms'
        conn.execute('UPDATE personal_agent_settings SET config=?,published_config=?',(json.dumps(config),json.dumps(config)))
    assert post_tool(client,db,kind='spam',urgency='urgent').status_code==200
    with db() as conn: assert conn.execute('SELECT count(*) n FROM personal_agent_notifications').fetchone()['n']==0


def test_notification_draft_is_not_active(workspace):
    client,db=workspace
    config=copy.deepcopy(pa.DEFAULT_CONFIG);config['notification_channel']='sms'
    assert client.put(admin_url('/config'),json={'config':config,'revision':1}).status_code==200
    assert post_tool(client,db,urgency='urgent').status_code==200
    assert client.get(admin_url()).json()['live_notification_channel']=='off'
    with db() as conn: assert conn.execute('SELECT count(*) n FROM personal_agent_notifications').fetchone()['n']==0
