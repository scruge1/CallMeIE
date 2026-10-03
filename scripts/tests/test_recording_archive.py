"""Regression: newer recordings outside the first lexical storage page."""
import ast
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recording_archive import latest_recording_objects
from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse


class Storage:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []
        self.signed_keys = []

    def get_paginator(self, operation):
        assert operation == 'list_objects_v2'
        return self

    def paginate(self, **kwargs):
        self.requests.append(kwargs)
        return iter(self.pages)

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        assert operation == 'get_object' and ExpiresIn == 3600
        self.signed_keys.append(Params['Key'])
        return 'https://example.invalid/fixture-audio'


def object_row(index):
    return {'Key': f'recordings/{index:04}.wav', 'Size': 10,
            'LastModified': datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=index)}


class RecordingArchiveTests(unittest.TestCase):
    def test_latest_beyond_first_eighty_is_included(self):
        storage = Storage([{'Contents': [object_row(i) for i in range(86)]}])
        result = latest_recording_objects(storage, 'fixture-bucket', 80)
        self.assertEqual(len(result), 80)
        self.assertEqual(result[0]['Key'], 'recordings/0085.wav')
        self.assertNotIn('recordings/0000.wav', [r['Key'] for r in result])

    def test_latest_on_later_page_and_out_of_key_order(self):
        first = object_row(1)
        newer = dict(object_row(2), Key='recordings/0000.wav')
        storage = Storage([{}, {'Contents': [first]}, {'Contents': [newer]}])
        result = latest_recording_objects(storage, 'fixture-bucket', 1)
        self.assertEqual(result, [newer])
        self.assertEqual(storage.requests[0]['Prefix'], 'recordings/')
        self.assertNotIn('MaxItems', storage.requests[0]['PaginationConfig'])

    def test_empty_folder_marker_and_other_prefix_are_excluded(self):
        storage = Storage([{'Contents': [{'Key': 'recordings/'}, {'Key': 'other/private.wav'}]}])
        self.assertEqual(latest_recording_objects(storage, 'fixture-bucket', 5), [])

    def test_stable_ties_and_missing_timestamp(self):
        one = object_row(1)
        two = dict(one, Key='recordings/z.wav')
        missing = {'Key': 'recordings/no-time.wav'}
        result = latest_recording_objects(Storage([{'Contents': [one, missing, two]}]), 'bucket', 3)
        self.assertEqual([x['Key'] for x in result], [two['Key'], one['Key'], missing['Key']])

    def test_invalid_limit_is_rejected_without_storage_request(self):
        storage = Storage([])
        for value in (0, -1, 501, True, '80'):
            with self.assertRaises(ValueError):
                latest_recording_objects(storage, 'bucket', value)
        self.assertEqual(storage.requests, [])

    def test_both_actual_endpoint_bodies_return_newest_and_sign_only_selected(self):
        for name in ('admin_recordings', 'admin_recordings_enriched'):
            with self.subTest(endpoint=name):
                storage = Storage([{'Contents': [object_row(i) for i in range(86)]}])
                response = self.invoke(name, storage, token='fixture-admin', limit=8)
                data = json.loads(response.body)
                self.assertEqual(data['count'], 8)
                self.assertEqual(data['recordings'][0]['key'], 'recordings/0085.wav')
                self.assertEqual(len(storage.signed_keys), 8)

    def test_actual_endpoint_auth_precedes_storage_access(self):
        for name in ('admin_recordings', 'admin_recordings_enriched'):
            storage = Storage([])
            with self.assertRaises(HTTPException) as raised:
                self.invoke(name, storage, token='wrong', limit=8)
            self.assertEqual(raised.exception.status_code, 401)
            self.assertEqual(storage.requests, [])

    def invoke(self, name, storage, *, token, limit):
        # Execute the exact maintained endpoint body with storage/DB fixtures;
        # avoid importing unrelated startup jobs or making provider calls.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        node.decorator_list = []
        ast.fix_missing_locations(node)
        def check_admin(value):
            if value != 'fixture-admin':
                raise HTTPException(401, 'Unauthorised')
        class Database:
            def execute(self, *args):
                return self
            def fetchall(self):
                return []
        @contextmanager
        def get_db():
            yield Database()
        boto = ModuleType('boto3')
        boto.client = lambda *args, **kwargs: storage
        botocore = ModuleType('botocore')
        config = ModuleType('botocore.config')
        config.Config = lambda **kwargs: kwargs
        namespace = {'Query': Query, 'HTTPException': HTTPException, 'JSONResponse': JSONResponse,
                     'check_admin': check_admin, 'os': os, 'get_db': get_db, 'json': json}
        env = {key: 'fixture' for key in ('HETZNER_OBJECT_STORAGE_ENDPOINT', 'HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID',
            'HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY', 'HETZNER_OBJECT_STORAGE_BUCKET')}
        with patch.dict(os.environ, env), patch.dict(sys.modules, {'boto3': boto, 'botocore': botocore, 'botocore.config': config}):
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<maintained-recordings-endpoint>', 'exec'), namespace)
            return asyncio.run(namespace[name](token=token, limit=limit))


if __name__ == '__main__':
    unittest.main()
