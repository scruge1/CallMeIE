"""Owner-configured routing survives normal configuration saves and publishes."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import personal_agent as pa


class PersonalHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        @contextmanager
        def db():
            conn = sqlite3.connect(Path(self.tmp.name) / "calls.db")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        self.db = db
        with db() as conn:
            conn.executescript(
                "CREATE TABLE call_events(id INTEGER PRIMARY KEY,created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
                "call_id TEXT,event_type TEXT,assistant TEXT,summary TEXT,detail TEXT);"
                "CREATE TABLE call_notes(id INTEGER PRIMARY KEY,created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
                "call_id TEXT,tenant_slug TEXT,note TEXT,actor TEXT);")

        def admin(token):
            if token != "test-admin":
                raise HTTPException(401)

        app = FastAPI()
        pa.install(app, db, admin)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.route = {"name": "example_helper", "trigger": "The caller asks for the example helper.",
                      "assistant_id": "11111111-2222-4333-8444-555555555555",
                      "spoken_ack": "I'll connect you to your helper now."}

    def url(self, path=""):
        return "/admin/api/personal-agent" + path + "?token=test-admin"

    def save(self, config, revision):
        return self.client.put(self.url("/config"), json={"config": config, "revision": revision})

    def test_default_and_legacy_configs_keep_existing_message_tools(self):
        config = copy.deepcopy(pa.DEFAULT_CONFIG)
        del config["assistant_handoffs"]
        model = pa.assistant_patch(config, "test-secret")["model"]
        self.assertEqual([item["type"] for item in model["tools"]], ["endCall", "function"])
        self.assertEqual(model["tools"][1]["function"]["name"], "savePersonalMessage")
        self.assertIn("send messages to callers, transfer calls or dispatch help", model["messages"][0]["content"])
        self.assertEqual(self.save(config, 1).status_code, 200)

    def test_handoff_survives_legacy_save_and_republish_without_losing_message_tools(self):
        config = copy.deepcopy(pa.DEFAULT_CONFIG)
        config["assistant_handoffs"] = [self.route]
        config["private_notes"] = "PRIVATE OPERATOR NOTE"
        self.assertEqual(self.save(config, 1).status_code, 200)
        sent = []

        async def provider(path, method="GET", payload=None):
            self.assertEqual(path, "assistant/" + pa.ASSISTANT_ID)
            if method == "PATCH":
                sent.append(copy.deepcopy(payload))
            response = copy.deepcopy(sent[-1])
            # Provider-added IDs/defaults must not create a false readback failure.
            response["model"]["tools"][-1]["id"] = "provider-added-id"
            return response

        with patch.object(pa, "provider_call", provider):
            self.assertTrue(self.client.post(self.url("/publish")).json()["published"])
            legacy = copy.deepcopy(config)
            del legacy["assistant_handoffs"]
            legacy["greeting"] = "Hello, who's calling?"
            self.assertEqual(self.save(legacy, 2).status_code, 200)
            self.assertTrue(self.client.post(self.url("/publish")).json()["published"])

        self.assertEqual(len(sent), 2)
        for published in sent:
            tools = published["model"]["tools"]
            self.assertEqual([item["type"] for item in tools], ["endCall", "function", "handoff"])
            self.assertEqual(tools[1]["function"]["name"], "savePersonalMessage")
            self.assertEqual(tools[2]["destinations"][0]["assistantId"], self.route["assistant_id"])
            self.assertEqual(tools[2]["messages"][0]["content"], self.route["spoken_ack"])
            self.assertNotIn("PRIVATE OPERATOR NOTE", json.dumps(published))
            prompt = published["model"]["messages"][0]["content"]
            self.assertIn("take precedence over the message-taking workflow", prompt)
            self.assertIn("handoff_example_helper", prompt)
            self.assertNotIn("send messages to callers, transfer calls or dispatch help", prompt)
        row = pa.settings(self.db)
        self.assertEqual(json.loads(row["published_config"])["assistant_handoffs"], [self.route])
        self.assertEqual(row["published_revision"], 3)

    def test_explicit_empty_routes_removes_handoff(self):
        config = copy.deepcopy(pa.DEFAULT_CONFIG)
        config["assistant_handoffs"] = [self.route]
        self.assertEqual(self.save(config, 1).status_code, 200)
        config["assistant_handoffs"] = []
        self.assertEqual(self.save(config, 2).status_code, 200)
        saved = json.loads(pa.settings(self.db)["config"])
        self.assertEqual(saved["assistant_handoffs"], [])
        self.assertEqual(len(pa.assistant_patch(saved, "test-secret")["model"]["tools"]), 2)

    def test_invalid_destinations_and_unapproved_fields_do_not_save(self):
        for invalid in [dict(self.route, assistant_id=pa.ASSISTANT_ID),
                        dict(self.route, assistant_id="+441234567890"),
                        dict(self.route, number="+441234567890"),
                        dict(self.route, name="caller-supplied destination")]:
            config = copy.deepcopy(pa.DEFAULT_CONFIG)
            config["assistant_handoffs"] = [invalid]
            self.assertEqual(self.save(config, 1).status_code, 422)
        self.assertEqual(pa.settings(self.db)["revision"], 1)

    def test_handoff_configuration_requires_existing_admin_auth(self):
        config = copy.deepcopy(pa.DEFAULT_CONFIG)
        config["assistant_handoffs"] = [self.route]
        result = self.client.put("/admin/api/personal-agent/config", json={"config": config, "revision": 1})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(pa.settings(self.db)["revision"], 1)

    def test_stripped_handoff_or_message_tool_fails_publish_readback(self):
        config = copy.deepcopy(pa.DEFAULT_CONFIG)
        config["assistant_handoffs"] = [self.route]
        self.assertEqual(self.save(config, 1).status_code, 200)
        for stripped in ["handoff", "function"]:
            sent = []

            async def provider(path, method="GET", payload=None):
                if method == "PATCH":
                    sent.append(copy.deepcopy(payload))
                    return payload
                response = copy.deepcopy(sent[-1])
                response["model"]["tools"] = [tool for tool in response["model"]["tools"]
                                               if tool["type"] != stripped]
                return response

            with patch.object(pa, "provider_call", provider):
                result = self.client.post(self.url("/publish"))
            self.assertEqual(result.status_code, 502)
            self.assertEqual(result.json()["detail"], "publish_readback_failed")
            self.assertEqual(pa.settings(self.db)["published_revision"], 0)


if __name__ == "__main__":
    unittest.main()
