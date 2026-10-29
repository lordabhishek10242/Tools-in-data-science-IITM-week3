"""Durable SQLite/PostgreSQL state for replay, user isolation and receipts."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DATABASE_PATH", "./invoice-agent.sqlite3")
        database_url = os.getenv("DATABASE_URL", "")
        self.postgres = database_url.startswith(("postgres://", "postgresql://"))
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            database_url = database_url.replace("postgres://", "postgresql://", 1)
            self.db = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
        schema = """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, principal TEXT NOT NULL, context_id TEXT NOT NULL,
                batch_id TEXT NOT NULL, state TEXT NOT NULL, task_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS tasks_by_owner ON tasks(principal, created_at DESC);
            CREATE TABLE IF NOT EXISTS messages (
                principal TEXT NOT NULL, message_id TEXT NOT NULL, semantic_hash TEXT NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(task_id), PRIMARY KEY(principal, message_id)
            );
            CREATE TABLE IF NOT EXISTS decisions (package_hash TEXT PRIMARY KEY, decision_json TEXT NOT NULL);
        """
        if self.postgres:
            self.db.execute(schema)
        else:
            self.db.executescript(schema)

    def _execute(self, query: str, params=()):
        return self.db.execute(query.replace("?", "%s") if self.postgres else query, params)

    def get_message(self, principal: str, message_id: str):
        return self._execute("SELECT semantic_hash, task_id FROM messages WHERE principal=? AND message_id=?",
                             (principal, message_id)).fetchone()

    def get_task(self, principal: str, task_id: str):
        row = self._execute("SELECT task_json FROM tasks WHERE principal=? AND task_id=?",
                            (principal, task_id)).fetchone()
        return json.loads(row["task_json"]) if row else None

    def get_any_task(self, task_id: str):
        return self._execute("SELECT principal FROM tasks WHERE task_id=?", (task_id,)).fetchone()

    def save_task(self, task: dict, principal: str, batch_id: str, message_id: str | None = None,
                  semantic_hash: str | None = None):
        self.db.execute("BEGIN" if self.postgres else "BEGIN IMMEDIATE")
        try:
            self._execute("""INSERT INTO tasks(task_id,principal,context_id,batch_id,state,task_json)
                VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                state=excluded.state, task_json=excluded.task_json""",
                (task["id"], principal, task["contextId"], batch_id,
                 task["status"]["state"], canonical_json(task)))
            if message_id is not None:
                self._execute("INSERT INTO messages(principal,message_id,semantic_hash,task_id) VALUES(?,?,?,?)",
                              (principal, message_id, semantic_hash, task["id"]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def list_tasks(self, principal: str):
        rows = self._execute("SELECT task_json FROM tasks WHERE principal=? ORDER BY created_at DESC, task_id DESC",
                             (principal,)).fetchall()
        return [json.loads(row["task_json"]) for row in rows]

    def get_decision(self, package_hash: str):
        row = self._execute("SELECT decision_json FROM decisions WHERE package_hash=?", (package_hash,)).fetchone()
        return json.loads(row["decision_json"]) if row else None

    def save_decision(self, package_hash: str, decision: dict):
        if self.postgres:
            self._execute("INSERT INTO decisions(package_hash,decision_json) VALUES(?,?) ON CONFLICT(package_hash) DO NOTHING",
                          (package_hash, canonical_json(decision)))
        else:
            self._execute("INSERT OR IGNORE INTO decisions(package_hash,decision_json) VALUES(?,?)",
                          (package_hash, canonical_json(decision)))

