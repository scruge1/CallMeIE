"""Private metadata/storage checks. No provider, credentials or live database."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from billing.status_events import normalize_status_event, store_status_event, read_status_feed


def message(status='in-progress'):
    return {'type': 'status-update', 'status': status, 'timestamp': 1760000400123,
            'call': {'id': 'fixture-call', 'assistantId': 'fixture-assistant',
                     'phoneNumberId': 'fixture-line', 'customer': {'number': 'private'}},
            'artifact': {'transcript': 'private'}, 'destination': {'number': 'private'}}


class StatusEvents(unittest.TestCase):
    def test_status_allowlist_and_metadata(self):
        for status in ('scheduled', 'queued', 'ringing', 'in-progress', 'forwarding', 'ended'):
            detail, encoded, key = normalize_status_event(message(status))
            self.assertEqual(detail['status'], status)
            self.assertNotIn('private', encoded)
            self.assertEqual(len(key), 64)

    def test_stable_key_ignores_artifacts(self):
        first = normalize_status_event(message())
        changed = message()
        changed['artifact']['transcript'] = 'different'
        changed['call']['customer']['number'] = 'different'
        self.assertEqual(first, normalize_status_event(changed))

    def test_status_timestamp_and_line_changes_are_distinct(self):
        original = normalize_status_event(message())[2]
        for path in ('status', 'timestamp', 'line'):
            changed = message()
            if path == 'status': changed['status'] = 'ended'
            elif path == 'timestamp': changed['timestamp'] += 1
            else: changed['call']['phoneNumberId'] = 'other-line'
            self.assertNotEqual(original, normalize_status_event(changed)[2])

    def test_invalid_shapes(self):
        for bad in (None, [], {}, {'type': 'status-update', 'call': []},
                    {'type': 'status-update', 'status': 'ended', 'call': {}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_status_event(bad)

    def test_invalid_status(self):
        for value in (None, [], 'completed', 'unknown'):
            bad = message(); bad['status'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_status_event(bad)

    def test_invalid_identifiers(self):
        for value in ([], True, 'x'*129, 'has space', 'has\nnewline'):
            bad = message(); bad['call']['id'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_status_event(bad)

    def test_invalid_timestamp(self):
        for value in (True, '1760000400123', float('nan'), float('inf'), -1, 10**17, 10**400):
            bad = message(); bad['timestamp'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_status_event(bad)

    def test_optional_fields_remain_unknown(self):
        detail, _, _ = normalize_status_event({'type':'status-update', 'status':'queued', 'call':{'id':'fixture'}})
        self.assertIsNone(detail['assistant_id'])
        self.assertIsNone(detail['line_id'])
        self.assertIsNone(detail['provider_timestamp'])

    def test_commit_failure_is_not_success(self):
        class Broken:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args): return self
            def fetchone(self): return {'id': 1}
            def commit(self): raise RuntimeError('synthetic commit failure')
        with self.assertRaises(RuntimeError):
            store_status_event(Broken, normalize_status_event(message()))

    def test_actual_sqlite_migration_and_concurrent_dedupe(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        import sqlalchemy as sa
        spec = importlib.util.spec_from_file_location('status_migration', ROOT/'alembic/versions/0012_call_status_event_key.py')
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        with tempfile.TemporaryDirectory(prefix='callmeie-status-test-') as directory:
            path = Path(directory)/'events.sqlite'
            engine = sa.create_engine('sqlite:///'+str(path))
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE call_events(id INTEGER PRIMARY KEY, created_at TEXT DEFAULT(datetime('now')),call_id TEXT,event_type TEXT,assistant TEXT,summary TEXT,detail TEXT)")
                conn.exec_driver_sql("INSERT INTO call_events(call_id,event_type) VALUES('old','call-ended')")
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
            engine.dispose()
            @contextmanager
            def connection():
                conn = sqlite3.connect(path, timeout=10)
                conn.row_factory = sqlite3.Row
                try:
                    with conn:
                        yield conn
                finally:
                    conn.close()
            normalized = normalize_status_event(message())
            self.assertEqual(read_status_feed(connection)['status'], 'no_events')
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: store_status_event(connection, normalized), range(8)))
            self.assertEqual(sum(r['stored'] for r in results), 1)
            self.assertEqual(sum(r['duplicate'] for r in results), 7)
            with connection() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM call_events').fetchone()[0], 2)
                self.assertIsNone(conn.execute("SELECT event_key FROM call_events WHERE call_id='old'").fetchone()[0])
            feed = read_status_feed(connection)
            self.assertEqual(feed['status'], 'observed')
            self.assertFalse(feed['coverage_verified'])
            self.assertIsInstance(feed['last_received_at'], float)

    def test_missing_unique_index_refuses_storage(self):
        @contextmanager
        def connection():
            conn = sqlite3.connect(':memory:')
            try:
                conn.execute('CREATE TABLE call_events(id INTEGER PRIMARY KEY,call_id TEXT,event_type TEXT,assistant TEXT,summary TEXT,detail TEXT,event_key TEXT)')
                yield conn
            finally:
                conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            store_status_event(connection, normalize_status_event(message()))

    def test_postgres_migration_compiles_offline(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        spec = importlib.util.spec_from_file_location('status_migration_pg', ROOT/'alembic/versions/0012_call_status_event_key.py')
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        output = io.StringIO()
        with Operations.context(MigrationContext.configure(dialect_name='postgresql', opts={'as_sql':True, 'output_buffer':output})):
            migration.upgrade()
        sql = output.getvalue()
        self.assertIn('ALTER TABLE call_events ADD COLUMN event_key TEXT', sql)
        self.assertIn('CREATE UNIQUE INDEX idx_call_events_event_key', sql)


if __name__ == '__main__': unittest.main()
