"""Automatic draft selection must not consume owner-commissioned events."""
import unittest
from tests.helpers import new_env
from bot.services.research import next_researched_event
from bot.worker import Worker
from bot.db import json_loads

class TopicAutoIsolationTests(unittest.TestCase):
    def test_auto_worker_does_not_retry_failed_manual_topic(self):
        env = new_env('parent-topic-isolation')
        env.ingest()
        row = env.db.fetchone('SELECT * FROM events ORDER BY id LIMIT 1')
        env.db.execute("UPDATE events SET event_key='topic_manual',status='researched',score=1000 WHERE id=?", (row['id'],))
        worker = Worker(env.config, db=env.db, clock=env.clock)
        self.assertIsNone(next_researched_event(env.db))
        self.assertEqual(worker.maybe_enqueue_draft(), 'no researched topic waiting')
        self.assertEqual(env.db.scalar("SELECT count(*) FROM jobs WHERE kind='draft'"), 0)
        automatic = dict(row)
        automatic.pop('id')
        automatic.update(event_key='evt_automatic', status='researched', score=0.1)
        columns=list(automatic)
        with env.db.tx() as conn:
            cursor=conn.execute('INSERT INTO events('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(automatic[k] for k in columns))
            automatic_id=cursor.lastrowid
        self.assertEqual(next_researched_event(env.db)['id'], automatic_id)
        worker.maybe_enqueue_draft()
        job=env.db.fetchone("SELECT * FROM jobs WHERE kind='draft'")
        self.assertEqual(json_loads(job['payload_json'],{})['event_id'], automatic_id)

if __name__=='__main__':
    unittest.main()
