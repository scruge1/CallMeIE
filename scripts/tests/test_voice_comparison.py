"""Test the exact audio endpoint without startup jobs or provider calls."""
import ast
import os
from pathlib import Path
import unittest
import wave

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.testclient import TestClient

SCRIPTS = Path(__file__).resolve().parents[1]


class VoiceComparisonTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((SCRIPTS / "server.py").read_text(encoding="utf-8-sig"))
        names = {"check_admin", "voice_comparison_audio", "tts_samples_api"}
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual(len(nodes), 3)
        app = FastAPI()
        namespace = {"app": app, "ADMIN_TOKEN": "fixture-only", "Query": Query,
                     "HTTPException": HTTPException, "FileResponse": FileResponse, "Request": Request, "Response": Response,
                     "os": os, "_SCRIPTS_DIR": str(SCRIPTS)}
        namespace.update({name: "fixture" for name in (
            "_TTS_LINE", "_TTS_KOKORO_B64", "_TTS_PREMIUM_B64", "_TTS_IRISH_B64", "_TTS_CLAIRE_B64")})
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<actual-voice-route>", "exec"), namespace)
        self.client = TestClient(app)

    def test_auth_required_for_all_ids(self):
        for model in ("eleven_flash_v2_5", "eleven_v4_turbo", "unknown"):
            for token in ("", "wrong"):
                r = self.client.get(f"/admin/api/voice-comparison/{model}", params={"token": token})
                self.assertEqual(r.status_code, 401)

    def test_exact_wavs_served(self):
        for model in ("eleven_flash_v2_5", "eleven_v4_turbo"):
            r = self.client.get(f"/admin/api/voice-comparison/{model}", params={"token": "fixture-only"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "audio/wav")
            self.assertEqual(r.headers["cache-control"], "private, no-store")
            file = SCRIPTS / "voice-samples" / f"{model}.wav"
            self.assertEqual(r.content, file.read_bytes())
            with wave.open(str(file)) as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 16000))
                self.assertGreater(audio.getnframes() / audio.getframerate(), 20)

    def test_unknown_and_path_like_ids_are_not_files(self):
        for model in ("unknown", "server.py", "..", "eleven_v4_turbo.wav"):
            r = self.client.get(f"/admin/api/voice-comparison/{model}", params={"token": "fixture-only"})
            self.assertEqual(r.status_code, 404)

    def test_ui_and_build_include_both_saved_samples(self):
        html = (SCRIPTS / "admin.html").read_text(encoding="utf-8-sig")
        self.assertIn('id="claireVoiceComparison"', html)
        for model in ("eleven_flash_v2_5", "eleven_v4_turbo"):
            self.assertIn("endpoint('/admin/api/voice-comparison/" + model + "', {format: 'mp3'})", html)
        self.assertIn('preload="metadata"', html)
        self.assertIn('COPY voice-samples ./voice-samples/', (SCRIPTS / "Dockerfile").read_text())

    def test_mobile_samples_use_exact_authenticated_saved_mp3s(self):
        for model in ("eleven_flash_v2_5", "eleven_v4_turbo"):
            route = '/admin/api/voice-comparison/' + model
            for token in ('', 'wrong'):
                self.assertEqual(self.client.get(route, params={'token':token,'format':'mp3'}).status_code,401)
            r = self.client.get(route, params={'token':'fixture-only','format':'mp3'})
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.headers['content-type'],'audio/mpeg')
            self.assertEqual(r.content, (SCRIPTS / 'voice-samples' / (model + '.mp3')).read_bytes())
            self.assertEqual(self.client.get(route, params={'token':'fixture-only','format':'../server.py'}).status_code,404)

    def test_mobile_byte_probes_seek_suffix_and_head(self):
        for model in ('eleven_flash_v2_5', 'eleven_v4_turbo'):
            for fmt in ('mp3', 'wav'):
                route = '/admin/api/voice-comparison/' + model
                params = {'token':'fixture-only', 'format':fmt}
                audio = (SCRIPTS / 'voice-samples' / (model + '.' + fmt)).read_bytes()
                for value, start, end in (('bytes=0-1',0,1),('bytes=44-',44,len(audio)-1),('bytes=-128',len(audio)-128,len(audio)-1),('bytes=0-9999999',0,len(audio)-1)):
                    r = self.client.get(route, params=params, headers={'Range':value})
                    self.assertEqual(r.status_code,206)
                    self.assertEqual(r.content,audio[start:end+1])
                    self.assertEqual(r.headers['content-range'],f'bytes {start}-{end}/{len(audio)}')
                    self.assertEqual(r.headers['accept-ranges'],'bytes')
                    self.assertEqual(int(r.headers['content-length']),end-start+1)
                head = self.client.head(route,params=params)
                self.assertEqual(head.status_code,200)
                self.assertEqual(head.content,b'')
                self.assertEqual(int(head.headers['content-length']),len(audio))
                self.assertEqual(self.client.head(route,params={'format':fmt}).status_code,401)

    def test_invalid_ranges_refused_and_if_range_not_assumed(self):
        route = '/admin/api/voice-comparison/eleven_v4_turbo'
        params = {'token':'fixture-only','format':'mp3'}
        for value in ('bytes=9999999-', 'bytes=3-1', 'bytes=-0', 'bytes=-', 'bytes=0-1,4-5', 'items=0-1', 'bytes='+'9'*200+'-'):
            r = self.client.get(route,params=params,headers={'Range':value})
            self.assertEqual(r.status_code,416)
            self.assertTrue(r.headers['content-range'].startswith('bytes */'))
        self.assertEqual(self.client.get(route,params=params,headers={'Range':'bytes=0-1','If-Range':'unknown'}).status_code,200)


if __name__ == "__main__":
    unittest.main()
