"""SQLite storage: tasks, the Notion page each one maps to, learned keywords, corrections."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    notion_page_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS keywords (
    category TEXT NOT NULL,
    keyword TEXT NOT NULL,
    PRIMARY KEY (category, keyword)
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notion_sync (
    task_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS notion_deletions (
    source_id TEXT PRIMARY KEY,
    page_id TEXT
);
CREATE TABLE IF NOT EXISTS capture_requests (
    user_id INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    text TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (user_id, request_id)
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Task:
    id: int
    user_id: int
    content: str
    category: str
    done: bool
    notion_page_id: str | None


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._in_transaction = False
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('instance_id', ?)", (str(uuid.uuid4()),))
            self.db.execute("INSERT OR IGNORE INTO notion_sync (task_id) SELECT id FROM tasks WHERE notion_page_id IS NULL")

    @contextmanager
    def transaction(self):
        """Group local task changes and their capture receipt into one durable commit."""
        if self._in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self._in_transaction = False

    def _commit(self):
        if not self._in_transaction:
            self.db.commit()

    def mark_pending(self, task_id: int):
        self.db.execute("""INSERT INTO notion_sync VALUES (?, 1)
            ON CONFLICT(task_id) DO UPDATE SET revision = revision + 1""", (task_id,))

    def source_id(self, task_id: int) -> str:
        instance = self.db.execute("SELECT value FROM metadata WHERE key = 'instance_id'").fetchone()[0]
        return f"{instance}:{task_id}"

    def capture_request(self, user_id: int, request_id: str):
        row = self.db.execute("SELECT text, result FROM capture_requests WHERE user_id = ? AND request_id = ?",
                              (user_id, request_id)).fetchone()
        return (row["text"], json.loads(row["result"])) if row else None

    def save_capture_request(self, user_id: int, request_id: str, text: str, result: dict):
        self.db.execute("INSERT INTO capture_requests VALUES (?, ?, ?, ?)",
                        (user_id, request_id, text, json.dumps(result)))
        self._commit()

    def pending_revision(self, task_id: int) -> int | None:
        row = self.db.execute("SELECT revision FROM notion_sync WHERE task_id = ?", (task_id,)).fetchone()
        return row[0] if row else None

    def acknowledge(self, task_id: int, revision: int):
        self.db.execute("DELETE FROM notion_sync WHERE task_id = ? AND revision = ?", (task_id, revision))
        self._commit()

    def close(self):
        self.db.close()

    def _task(self, row) -> Task:
        return Task(row["id"], row["user_id"], row["content"], row["category"],
                    bool(row["done"]), row["notion_page_id"])

    def add_task(self, user_id: int, content: str, category: str) -> Task:
        with self.transaction():
            cur = self.db.execute(
                "INSERT INTO tasks (user_id, content, category) VALUES (?, ?, ?)",
                (user_id, content, category),
            )
            self.mark_pending(cur.lastrowid)
        return self.get(cur.lastrowid)

    def get(self, task_id: int) -> Task | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def set_notion_page(self, task_id: int, page_id: str) -> None:
        self.db.execute("UPDATE tasks SET notion_page_id = ? WHERE id = ?", (page_id, task_id))
        self._commit()

    def set_category(self, task_id: int, category: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE tasks SET category = ? WHERE id = ?", (category, task_id))
            self.mark_pending(task_id)

    def set_done(self, task_id: int, done: bool = True) -> None:
        with self.transaction():
            self.db.execute("UPDATE tasks SET done = ? WHERE id = ?", (int(done), task_id))
            self.mark_pending(task_id)

    def delete(self, task_id: int) -> None:
        with self.transaction():
            task = self.get(task_id)
            if task:
                self.db.execute("INSERT OR IGNORE INTO notion_deletions VALUES (?, ?)",
                                (self.source_id(task_id), task.notion_page_id))
            self.db.execute("DELETE FROM notion_sync WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def open_tasks(self, user_id: int, category: str | None = None) -> list[Task]:
        q, args = "SELECT * FROM tasks WHERE user_id = ? AND done = 0", [user_id]
        if category:
            q += " AND category = ?"
            args.append(category)
        q += " ORDER BY category, id"
        return [self._task(r) for r in self.db.execute(q, args)]

    def add_keyword(self, category: str, keyword: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO keywords VALUES (?, ?)", (category, keyword.lower()))
        self._commit()

    def keywords(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.db.execute("SELECT category, keyword FROM keywords ORDER BY keyword"):
            out.setdefault(r["category"], []).append(r["keyword"])
        return out

    def add_correction(self, text: str, category: str) -> None:
        self.db.execute("INSERT INTO corrections (text, category) VALUES (?, ?)", (text, category))
        self._commit()

    def corrections(self) -> list[tuple[str, str]]:
        return [(r["text"], r["category"]) for r in self.db.execute("SELECT text, category FROM corrections")]
