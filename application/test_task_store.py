"""task_store (DynamoDB) behaviour. Needs DynamoDB Local:

    docker run -d -p 8000:8000 amazon/dynamodb-local -jar DynamoDBLocal.jar -sharedDb -inMemory
    DYNAMODB_ENDPOINT_URL=http://localhost:8000 python -m unittest application.test_task_store
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid

ENDPOINT = (os.environ.get("DYNAMODB_ENDPOINT_URL") or "").strip()


@unittest.skipUnless(ENDPOINT, "DYNAMODB_ENDPOINT_URL (DynamoDB Local) not set")
class TaskStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("AWS_REGION", "us-west-2")
        os.environ["TASK_DB_PROJECT"] = f"test-{uuid.uuid4().hex[:8]}"
        from application import task_store

        cls.ts = task_store
        task_store.init_db()

    def setUp(self):
        self.user = f"{uuid.uuid4().hex[:8]}@example.com"

    def test_user_isolation(self):
        t = self.ts.create_task(self.user)
        self.assertIsNone(self.ts.get_task(t["id"], "other@example.com"))
        self.ts.add_message(t["id"], "user", "hi", user_id=self.user)
        self.assertEqual(self.ts.list_messages(t["id"], "other@example.com"), [])

    def test_stale_writer_cannot_revert_other_fields(self):
        """Two writers touching different fields must both survive (the SQLite bug)."""
        t = self.ts.create_task(self.user)
        self.ts.update_task(t["id"], self.user, pinned=True)
        self.ts.update_task(t["id"], self.user, title="renamed")
        got = self.ts.get_task(t["id"], self.user)
        self.assertTrue(got["pinned"])
        self.assertEqual(got["title"], "renamed")

    def test_first_user_message_sets_title_once(self):
        t = self.ts.create_task(self.user)
        self.ts.add_message(t["id"], "user", "first question", user_id=self.user)
        self.ts.add_message(t["id"], "user", "second", user_id=self.user)
        self.assertEqual(self.ts.get_task(t["id"], self.user)["title"], "first question")

    def test_update_all_tasks_and_list_order(self):
        a = self.ts.create_task(self.user)
        b = self.ts.create_task(self.user)
        self.ts.update_task(a["id"], self.user, pinned=True)
        self.assertEqual(
            self.ts.update_all_tasks(self.user, model_name="M", memory_enabled=False), 2
        )
        tasks = self.ts.list_tasks(self.user)
        self.assertEqual(tasks[0]["id"], a["id"])
        self.assertTrue(all(t["model_name"] == "M" and not t["memory_enabled"] for t in tasks))
        self.assertEqual(b["runtime_session_id"], b["id"])

    def test_oversized_message_is_truncated_not_rejected(self):
        t = self.ts.create_task(self.user)
        m = self.ts.add_message(t["id"], "user", "x" * 500_000, user_id=self.user)
        self.assertLessEqual(len(m["content"].encode()), 180_000)

    def test_delete_task_removes_messages(self):
        t = self.ts.create_task(self.user)
        self.ts.add_message(t["id"], "user", "hi", user_id=self.user)
        self.assertTrue(self.ts.delete_task(t["id"], self.user))
        self.assertFalse(self.ts.delete_task(t["id"], self.user))
        self.assertEqual(self.ts.list_messages(t["id"], self.user), [])

    def test_fingerprint_and_export(self):
        t = self.ts.create_task(self.user)
        self.ts.add_message(t["id"], "user", "q", user_id=self.user, images=["a.png"])
        self.ts.add_message(t["id"], "assistant", "a", user_id=self.user)
        fp = self.ts.get_message_fingerprint(self.user)
        self.assertEqual(fp["message_count"], 2)
        self.assertIsNotNone(fp["max_created_at"])
        with tempfile.TemporaryDirectory() as d:
            path = self.ts.export_user_sqlite(self.user, os.path.join(d, "x.db"))
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
