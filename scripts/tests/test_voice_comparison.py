"""Test the exact audio endpoint without startup jobs or provider calls."""
import ast
from contextvars import ContextVar
import hmac
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
        names = {"check_admin", "voice_comparison_audio", "voice_reference_audio", "_saved_audio_response", "tts_samples_api"}
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual(len(nodes), 5)
        app = FastAPI()
        namespace = {"app": app, "ADMIN_TOKEN": "fixture-only", "Query": Query,
                     "HTTPException": HTTPException, "FileResponse": FileResponse, "Request": Request, "Response": Response,
                     "os": os, "_SCRIPTS_DIR": str(SCRIPTS),
                     "hmac": hmac, "_admin_bearer": ContextVar("fixture_admin_bearer", default=None)}
        namespace.update({name: "fixture" for name in (
            "_TTS_LINE", "_TTS_KOKORO_B64", "_TTS_PREMIUM_B64", "_TTS_IRISH_B64", "_TTS_CLAIRE_B64")})
        import base64
        self.reference_bytes = b'ID3' + b'fixture-audio' * 20
        namespace.update({name:base64.b64encode(self.reference_bytes).decode() for name in
            ('_TTS_KOKORO_B64','_TTS_PREMIUM_B64','_TTS_IRISH_B64','_TTS_CLAIRE_B64')})
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

    def test_breeze_private_sample_auth_bytes_range_head_and_label(self):
        import hashlib
        route = '/admin/api/voice-comparison/breeze-irish-internal'
        for fmt, digest in (
            ('wav', '6cabe452a3ca938da692d915add98a3f5aedfa85610e8840fdc9ae0aace1e1eb'),
            ('mp3', 'c1a4027b6176fed56a683b3eb844df4df6d2415105dfd207401e642ce554b576'),
        ):
            expected = (SCRIPTS / 'voice-samples' / ('breeze-irish-internal.' + fmt)).read_bytes()
            self.assertEqual(hashlib.sha256(expected).hexdigest(), digest)
            params = {'token': 'fixture-only', 'format': fmt}
            for token in ('', 'wrong'):
                self.assertEqual(self.client.get(route, params={'token': token, 'format': fmt}).status_code, 401)
                self.assertEqual(self.client.head(route, params={'token': token, 'format': fmt}).status_code, 401)
            full = self.client.get(route, params=params)
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.content, expected)
            self.assertEqual(full.headers['cache-control'], 'private, no-store')
            self.assertEqual(full.headers['content-type'], 'audio/mpeg' if fmt == 'mp3' else 'audio/wav')
            for header, start, end in (('bytes=0-1', 0, 1), ('bytes=-128', len(expected)-128, len(expected)-1)):
                ranged = self.client.get(route, params=params, headers={'Range': header})
                self.assertEqual(ranged.status_code, 206)
                self.assertEqual(ranged.content, expected[start:end+1])
                self.assertEqual(ranged.headers['content-range'], f'bytes {start}-{end}/{len(expected)}')
                self.assertEqual(ranged.headers['cache-control'], 'private, no-store')
            head = self.client.head(route, params=params)
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b'')
            self.assertEqual(int(head.headers['content-length']), len(expected))
        html = (SCRIPTS / 'admin.html').read_text(encoding='utf-8-sig')
        self.assertIn('id="breezePrivateSample"', html)
        self.assertIn('Breeze TTS2 - private Irish voice test', html)
        self.assertIn('Internal evaluation only; not enabled on phone assistants.', html)
        self.assertIn("endpoint('/admin/api/voice-comparison/breeze-irish-internal', {format: 'mp3'})", html)

    def test_hard_irish_player_label_order_and_auth(self):
        import hashlib
        import re
        route = '/admin/api/voice-comparison/breeze-hard-irish'
        for fmt, digest in (
            ('mp3', 'a697ebfc49128f5ed60d120f4c1919198f67ab9ed2c2e5baff68fb6f61b0a4fe'),
            ('wav', '6d756275cb1768f90a440b7c6b66280ff5a1e1b51e047b5f7e90a27ab8e46e5d'),
        ):
            expected = (SCRIPTS / 'voice-samples' / ('breeze-hard-irish.' + fmt)).read_bytes()
            self.assertEqual(hashlib.sha256(expected).hexdigest(), digest)
            for token in ('', 'wrong'):
                for method in ('GET', 'HEAD'):
                    response = self.client.request(method, route, params={'token': token, 'format': fmt})
                    self.assertEqual(response.status_code, 401)
            params = {'token': 'fixture-only', 'format': fmt}
            full = self.client.get(route, params=params)
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.content, expected)
            self.assertEqual(full.headers['cache-control'], 'private, no-store')
            self.assertEqual(full.headers['content-type'], 'audio/mpeg' if fmt == 'mp3' else 'audio/wav')
            for header, start, end in (('bytes=0-1', 0, 1), ('bytes=-128', len(expected)-128, len(expected)-1)):
                ranged = self.client.get(route, params=params, headers={'Range': header})
                self.assertEqual(ranged.status_code, 206)
                self.assertEqual(ranged.content, expected[start:end+1])
                self.assertEqual(ranged.headers['content-range'], f'bytes {start}-{end}/{len(expected)}')
                self.assertEqual(ranged.headers['cache-control'], 'private, no-store')
            head = self.client.head(route, params=params)
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b'')
            self.assertEqual(int(head.headers['content-length']), len(expected))
            self.assertEqual(head.headers['cache-control'], 'private, no-store')
        with wave.open(str(SCRIPTS / 'voice-samples' / 'breeze-hard-irish.wav')) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 36.08, places=2)
        html = (SCRIPTS / 'admin.html').read_text(encoding='utf-8-sig')
        self.assertIn('Breeze TTS2 - difficult Irish names and places', html)
        self.assertIn("endpoint('/admin/api/voice-comparison/breeze-hard-irish', {format: 'mp3'})", html)
        self.assertLess(html.index('id="breezePrivateSample"'), html.index('id="breezeHardIrishSample"'))
        self.assertLess(html.index('id="breezeHardIrishSample"'), html.index('${refRow("Kokoro"'))
        self.assertIn('<summary>Read test script</summary>', html)
        script = re.search(r'<p id="breezeHardIrishScript">([^<]+)</p>', html).group(1)
        self.assertEqual(hashlib.sha256(script.encode('utf-8')).hexdigest(), '5398dc13697c5b1f1e189b9927736968ce05f5fe9fba9ed9d62c9ed88f44b5d4')

    def test_saved_hotel_pairs_auth_bytes_head_and_range(self):
        for situation in ('routing', 'privacy', 'complaint', 'clarity'):
            for delivery in ('plain', 'directed'):
                sample = f'hotel-{situation}-{delivery}'
                route = '/admin/api/voice-comparison/' + sample
                expected = (SCRIPTS / 'voice-samples' / (sample + '.mp3')).read_bytes()
                self.assertGreater(len(expected), 1000)
                for token in ('', 'wrong'):
                    self.assertEqual(self.client.get(route, params={'token':token,'format':'mp3'}).status_code,401)
                params = {'token':'fixture-only','format':'mp3'}
                r = self.client.get(route, params=params)
                self.assertEqual(r.status_code,200)
                self.assertEqual(r.content,expected)
                self.assertEqual(r.headers['content-type'],'audio/mpeg')
                self.assertEqual(r.headers['cache-control'],'private, no-store')
                ranged = self.client.get(route,params=params,headers={'Range':'bytes=0-1'})
                self.assertEqual(ranged.status_code,206)
                self.assertEqual(ranged.content,expected[:2])
                head = self.client.head(route,params=params)
                self.assertEqual(head.content,b'')
                self.assertEqual(int(head.headers['content-length']),len(expected))
                self.assertEqual(self.client.get(route,params={'token':'fixture-only','format':'wav'}).status_code,404)

    def test_hotel_comparison_reuses_saved_audio_player(self):
        html = (SCRIPTS / 'admin.html').read_text(encoding='utf-8-sig')
        self.assertIn('id="hotelVoiceComparison"',html)
        self.assertIn("endpoint('/admin/api/voice-comparison/hotel-' + key + '-plain', {format: 'mp3'})",html)
        self.assertIn("endpoint('/admin/api/voice-comparison/hotel-' + key + '-directed', {format: 'mp3'})",html)

    def test_ui_and_build_include_both_saved_samples(self):
        html = (SCRIPTS / "admin.html").read_text(encoding="utf-8-sig")
        self.assertIn('id="claireVoiceComparison"', html)
        for model in ("eleven_flash_v2_5", "eleven_v4_turbo"):
            self.assertIn("endpoint('/admin/api/voice-comparison/" + model + "', {format: 'mp3'})", html)
        self.assertIn('preload="metadata"', html)
        self.assertIn('COPY voice-samples ./voice-samples/', (SCRIPTS / "Dockerfile").read_text())

    def test_phone_voice_picker_and_reset_use_v4(self):
        text = (SCRIPTS / "server.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        default = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'OBRIEN_DEFAULT_VOICE' for t in n.targets))
        self.assertEqual(ast.literal_eval(default.value)['model'], 'eleven_v4_turbo')
        picker = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'client_set_voice')
        self.assertIn('"model": "eleven_v4_turbo"', ast.get_source_segment(text, picker))
        preview = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'client_voice_preview')
        self.assertIn('"model_id": "eleven_v4_turbo"', ast.get_source_segment(text, preview))
        html = (SCRIPTS / 'admin.html').read_text(encoding='utf-8-sig')
        self.assertIn('<option value="eleven_v4_turbo">V4 Turbo (CallMeIE standard)</option>', html)

    def test_saved_references_auth_exact_bytes_range_head_and_allowlist(self):
        for sample in ('kokoro','premium','irish','claire'):
            url='/admin/api/voice-reference/'+sample
            self.assertEqual(self.client.get(url).status_code,401)
            r=self.client.get(url,params={'token':'fixture-only'})
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.content,self.reference_bytes)
            self.assertEqual(r.headers['content-type'],'audio/mpeg')
            r=self.client.get(url,params={'token':'fixture-only'},headers={'Range':'bytes=0-1'})
            self.assertEqual(r.status_code,206)
            self.assertEqual(r.content,self.reference_bytes[:2])
            r=self.client.head(url,params={'token':'fixture-only'})
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.content,b'')
            self.assertEqual(int(r.headers['content-length']),len(self.reference_bytes))
        self.assertEqual(self.client.get('/admin/api/voice-reference/server.py',params={'token':'fixture-only'}).status_code,404)

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
