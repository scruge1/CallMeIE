"""Execute real handler bodies with SQLite and mocked outbound services."""
import ast
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import AsyncMock, Mock
from fastapi import BackgroundTasks, Request
from fastapi.responses import JSONResponse
from billing.status_events import store_final_report


class DemoPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE leads (id INTEGER PRIMARY KEY, call_id TEXT, name TEXT,
          phone TEXT, business_type TEXT, interest TEXT, source TEXT, callback_requested INTEGER DEFAULT 0,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, demo_completed INTEGER, topics_discussed TEXT,
          interest_level TEXT, pain_point TEXT, estimated_missed_calls_per_week TEXT, next_action TEXT);
          CREATE TABLE call_events (id INTEGER PRIMARY KEY,call_id TEXT,event_type TEXT,
            assistant TEXT,summary TEXT,detail TEXT,event_key TEXT UNIQUE);''')
        @contextmanager
        def get_db():
            yield self.db
        async def fixture_threadpool(function, *args):
            # Keep this AST fixture's in-memory SQLite on its creating thread.
            # Real offloading is exercised by the separate imported-app fixture.
            return function(*args)
        self.sms = AsyncMock()
        self.calendar = Mock()
        self.events = []
        def log_event(*args):
            self.events.append(args)
        namespace = dict(Request=Request, BackgroundTasks=BackgroundTasks, JSONResponse=JSONResponse,
            get_db=get_db, send_sms=self.sms, send_telegram=AsyncMock(), log_event=log_event,
            get_client=lambda _: {'name':'Demo','owner':'owner','from':'sender'}, OWNER_NUMBER='owner',
            TWILIO_FROM='sender', DEMO_ASSISTANT_IDS={'claire':'demo'},
            CALLMEIE_CALLBACK_CALENDAR_ID='fixture', create_callback_event=self.calendar,
            _mirror_recording_to_hetzner=Mock(), _delayed_mirror_via_vapi=Mock(), json=json,
            run_in_threadpool=fixture_threadpool,store_final_report=store_final_report)
        source = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        names = {'call_ended','capture_lead','demo_complete','_parse_vapi_tool_call','_vapi_result','_capture_lead_phone'}
        nodes = [n for n in source.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        for node in nodes:
            node.decorator_list = []
        exec(compile(ast.Module(body=nodes,type_ignores=[]), 'real-server-handlers', 'exec'), namespace)
        self.ns = namespace

    def tearDown(self):
        self.db.close()

    def request(self, args=None, assistant='claire', call_id='fictional-call', duration=60):
        body = {'message': {'type':'end-of-call-report', 'call': {'id':call_id,'assistantId':assistant,
                'status':'ended','duration':duration,'customer':{'number':'fictional-caller'}},
                'toolCallList':[{'id':'tool-fixture','function':{'arguments':args or {}}}]}}
        async def receive():
            return {'type':'http.request','body':json.dumps(body).encode()}
        return Request({'type':'http','method':'POST','path':'/'},receive)

    def test_demo_end_never_sends_unsolicited_sms(self):
        result = asyncio.run(self.ns['call_ended'](self.request(), BackgroundTasks()))
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM call_events WHERE event_type='call-ended'").fetchone()[0],1)
        self.sms.assert_not_called()
        self.assertTrue(any(e[1]=='demo-follow-up-suppressed' for e in self.events))

    def test_handoff_demo_without_origin_mapping_also_suppresses(self):
        request = self.request(assistant='unmapped-origin')
        body = asyncio.run(request.json())
        body['message']['artifact']={'assistantActivations':[{'assistantId':'claire'}]}
        async def receive(): return {'type':'http.request','body':json.dumps(body).encode()}
        result = asyncio.run(self.ns['call_ended'](Request({'type':'http'},receive),BackgroundTasks()))
        self.assertEqual(result.status_code,200)
        self.sms.assert_not_called()

    def test_curiosity_and_string_true_do_not_create_callback(self):
        for permission in (None,False,'true','false',1):
            asyncio.run(self.ns['demo_complete'](self.request({'interest_level':'curious','callback_requested':permission})))
        self.calendar.assert_not_called()
        self.assertTrue(all(call.args[0]=='owner' for call in self.sms.call_args_list))

    def test_explicit_callback_request_creates_normal_callback(self):
        self.calendar.return_value={'id':'fixture-event'}
        asyncio.run(self.ns['capture_lead'](self.request({'source':'demo','callback_requested':True})))
        row=self.db.execute('SELECT * FROM leads').fetchone()
        self.assertEqual(row['callback_requested'],1)
        self.assertEqual(row['name'],'')
        self.assertEqual(row['business_type'],'')
        asyncio.run(self.ns['demo_complete'](self.request({'interest_level':'curious','callback_requested':True})))
        self.calendar.assert_called_once()
        self.assertTrue(all(call.args[0]=='owner' for call in self.sms.call_args_list))

    def test_missing_permission_defaults_false_in_lead(self):
        asyncio.run(self.ns['capture_lead'](self.request({'source':'demo'})))
        self.assertEqual(self.db.execute('SELECT callback_requested FROM leads').fetchone()[0],0)
        self.assertIn('No contact permission',self.sms.call_args.args[1])

if __name__=='__main__': unittest.main()
