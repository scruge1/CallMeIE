"""Final report transactions in disposable SQLite. No provider or live data."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billing.status_events import store_final_report


class FinalReports(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='callmeie-final-report-')
        self.path = Path(self.folder.name) / 'events.sqlite'
        with self.connection() as db:
            db.execute('CREATE TABLE call_events(id INTEGER PRIMARY KEY,call_id TEXT,event_type TEXT,assistant TEXT,summary TEXT,detail TEXT,event_key TEXT UNIQUE)')
            db.commit()
        self.rows = [('origin', 'Synthetic summary', {'transcript': 'synthetic'}),
                     ('handoff', 'Synthetic summary', {'is_mirror_row': True})]

    def tearDown(self):
        self.folder.cleanup()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def test_rows_commit_together(self):
        result = store_final_report(self.connection, 'fixture', self.rows)
        self.assertTrue(result['stored'])
        with self.connection() as db:
            rows = db.execute('SELECT assistant,detail,event_key FROM call_events ORDER BY id').fetchall()
        self.assertEqual([row[0] for row in rows], ['origin', 'handoff'])
        self.assertEqual(json.loads(rows[0][1]), self.rows[0][2])
        self.assertEqual(len(rows[0][2]), 64)
        self.assertIsNone(rows[1][2])

    def test_concurrent_duplicate_reports(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: store_final_report(self.connection, 'fixture', self.rows), range(8)))
        self.assertEqual(sum(r['stored'] for r in results), 1)
        self.assertEqual(sum(r['duplicate'] for r in results), 7)
        with self.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM call_events').fetchone()[0], 2)

    def test_historical_null_key_remains_duplicate(self):
        with self.connection() as db:
            db.execute("INSERT INTO call_events(call_id,event_type) VALUES('fixture','call-ended')")
        self.assertTrue(store_final_report(self.connection, 'fixture', self.rows)['duplicate'])
        with self.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM call_events').fetchone()[0], 1)

    def test_changed_assistant_cannot_replay(self):
        store_final_report(self.connection, 'fixture', self.rows)
        self.assertTrue(store_final_report(self.connection, 'fixture', [('changed', 'new', {})])['duplicate'])

    def test_handoff_failure_rolls_back_origin(self):
        with self.connection() as db:
            db.execute("CREATE TRIGGER fail_mirror BEFORE INSERT ON call_events WHEN NEW.assistant='handoff' BEGIN SELECT RAISE(ABORT,'synthetic mirror failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store_final_report(self.connection, 'fixture', self.rows)
        with self.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM call_events').fetchone()[0], 0)
            db.execute('DROP TRIGGER fail_mirror')
        self.assertTrue(store_final_report(self.connection, 'fixture', self.rows)['stored'])

    def test_explicit_commit_failure_propagates(self):
        class Broken:
            reads = 0
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args): self.reads += 1; return self
            def fetchone(self): return None if self.reads == 1 else (1,)
            def commit(self): raise RuntimeError('synthetic commit failure')
        with self.assertRaises(RuntimeError):
            store_final_report(Broken, 'fixture', self.rows[:1])

    def test_status_rows_do_not_block_final_report(self):
        with self.connection() as db:
            db.execute("INSERT INTO call_events(call_id,event_type) VALUES('fixture','call-status')")
        self.assertTrue(store_final_report(self.connection, 'fixture', self.rows)['stored'])

    def test_missing_identity_refused(self):
        for call_id in (None, '', [], 'with space'):
            with self.subTest(call_id=call_id), self.assertRaises(ValueError):
                store_final_report(self.connection, call_id, self.rows)

    def test_empty_rows_refused(self):
        with self.assertRaises(ValueError):
            store_final_report(self.connection, 'fixture', [])

    def test_missing_unique_index_refuses_storage(self):
        with self.connection() as db:
            db.execute('DROP TABLE call_events')
            db.execute('CREATE TABLE call_events(id INTEGER PRIMARY KEY,call_id TEXT,event_type TEXT,assistant TEXT,summary TEXT,detail TEXT,event_key TEXT)')
        with self.assertRaises(sqlite3.OperationalError):
            store_final_report(self.connection, 'fixture', self.rows)
        with self.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM call_events').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
