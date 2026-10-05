"""Use actual preview functions with one fake provider, never paid synthesis."""
import ast
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, JSONResponse
from fastapi.testclient import TestClient

SCRIPTS = Path(__file__).resolve().parents[1]


class PreviewAudioTests(unittest.TestCase):
    def setUp(self):
        source = (SCRIPTS / 'server.py').read_text(encoding='utf-8-sig')
        names = {'check_admin', '_saved_audio_response', '_prune_preview_audio',
                 '_preview_audio_response', 'preview_audio', 'tts_preview'}
        nodes = [n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual(len(nodes), len(names))
        self.ns = {'app':FastAPI(), 'ADMIN_TOKEN':'fixture-only', 'Query':Query,
                   'Request':Request, 'Response':Response, 'JSONResponse':JSONResponse,
                   'HTTPException':HTTPException, 'os':os, '_TTS_PREVIEW_AUDIO':{},
                   '_TTS_LINE':'Fixture speech', '_TTS_PRESETS':{'premium':('fixture',None,None)}}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<actual-preview>', 'exec'),self.ns)
        self.client = TestClient(self.ns['app'])
        self.audio = b'ID3' + b'fixture-audio' * 20

    def store(self, audio=None):
        return self.ns['_preview_audio_response'](audio or self.audio).headers['x-preview-audio-id']

    def test_replay_auth_range_head_and_unknown(self):
        sample = self.store()
        url = '/admin/api/tts-preview-audio/'+sample
        self.assertEqual(self.client.get(url).status_code,401)
        self.assertEqual(self.client.head(url).status_code,401)
        self.assertEqual(self.client.get(url,params={'token':'wrong'}).status_code,401)
        r=self.client.get(url,params={'token':'fixture-only'})
        self.assertEqual(r.content,self.audio)
        self.assertEqual(r.headers['cache-control'],'private, no-store')
        for value, data in [('bytes=0-1',self.audio[:2]),('bytes=-8',self.audio[-8:])]:
            r=self.client.get(url,params={'token':'fixture-only'},headers={'Range':value})
            self.assertEqual(r.status_code,206)
            self.assertEqual(r.content,data)
        r=self.client.head(url,params={'token':'fixture-only'})
        self.assertEqual(r.content,b'')
        self.assertEqual(int(r.headers['content-length']),len(self.audio))
        self.assertEqual(self.client.get('/admin/api/tts-preview-audio/unknown',params={'token':'fixture-only'}).status_code,404)

    def test_expiry_no_regeneration(self):
        sample=self.store()
        self.ns['_TTS_PREVIEW_AUDIO'][sample]=(0,self.audio)
        r=self.client.get('/admin/api/tts-preview-audio/'+sample,params={'token':'fixture-only'})
        self.assertEqual(r.status_code,404)
        self.assertNotIn(sample,self.ns['_TTS_PREVIEW_AUDIO'])

    def test_count_byte_caps_and_empty_or_oversized_refusal(self):
        first=self.store()
        for _ in range(10):self.store()
        self.assertEqual(len(self.ns['_TTS_PREVIEW_AUDIO']),10)
        self.assertNotIn(first,self.ns['_TTS_PREVIEW_AUDIO'])
        self.ns['_TTS_PREVIEW_AUDIO'].clear()
        for _ in range(6):self.store(b'x'*(2*1024*1024))
        self.assertEqual(len(self.ns['_TTS_PREVIEW_AUDIO']),5)
        for audio in (b'',b'x'*(2*1024*1024+1)):
            with self.assertRaises(HTTPException):self.ns['_preview_audio_response'](audio)

    def test_actual_generation_once_repeated_replay_never_calls_provider(self):
        calls=[]
        audio=self.audio
        class FakeCommunicate:
            def __init__(self,*args,**kwargs):calls.append(1)
            async def stream(self):yield {'type':'audio','data':audio}
        with patch.dict('sys.modules',{'edge_tts':types.SimpleNamespace(Communicate=FakeCommunicate)}):
            denied=self.client.get('/admin/api/tts-preview')
            self.assertEqual(denied.status_code,401)
            r=self.client.get('/admin/api/tts-preview',params={'token':'fixture-only','engine':'edge'})
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.content,self.audio)
            sample=r.headers['x-preview-audio-id']
            for _ in range(4):
                replay=self.client.get('/admin/api/tts-preview-audio/'+sample,params={'token':'fixture-only'},headers={'Range':'bytes=0-1'})
                self.assertEqual(replay.status_code,206)
        self.assertEqual(len(calls),1)

    def test_ui_uses_authenticated_audio_id_not_blob(self):
        html=(SCRIPTS/'admin.html').read_text(encoding='utf-8-sig')
        handler=html.split('window.ttsPreview = async (eng) => {',1)[1].split('const refRow',1)[0]
        self.assertNotIn('createObjectURL',handler)
        self.assertIn('endpoint("/admin/api/tts-preview-audio/" + sampleId)',handler)
        self.assertIn('if (previewBusy) return',handler)
        self.assertIn('finally',handler)


if __name__=='__main__':unittest.main()
