"""Test the exact audio endpoint without startup jobs or provider calls."""
import ast
import base64
import os
from pathlib import Path
import unittest
import wave

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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
                     "HTTPException": HTTPException, "FileResponse": FileResponse,
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
            self.assertIn('ref.claire_' + model, html)
        self.assertIn('preload="metadata"', html)
        self.assertIn('COPY voice-samples ./voice-samples/', (SCRIPTS / "Dockerfile").read_text())

    def test_embedded_mobile_samples_use_exact_saved_mp3s(self):
        for token in ("", "wrong"):
            self.assertEqual(self.client.get('/admin/api/tts-samples', params={'token': token}).status_code, 401)
        r = self.client.get('/admin/api/tts-samples', params={'token': 'fixture-only'})
        self.assertEqual(r.status_code, 200)
        for model in ("eleven_flash_v2_5", "eleven_v4_turbo"):
            uri = r.json()['claire_' + model]
            prefix, payload = uri.split(',', 1)
            self.assertEqual(prefix, 'data:audio/mpeg;base64')
            self.assertEqual(base64.b64decode(payload), (SCRIPTS / 'voice-samples' / (model + '.mp3')).read_bytes())


if __name__ == "__main__":
    unittest.main()
