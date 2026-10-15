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
CREATE TABLE IF NOT EXISTS notion_title_sync (
    task_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notion_date_sync (
    task_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL
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
CREATE TABLE IF NOT EXISTS speech_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    original TEXT NOT NULL,
    intended TEXT NOT NULL,
    category TEXT,
    context TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS speech_memory_user ON speech_memory(user_id, active);
CREATE TABLE IF NOT EXISTS speech_captures (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    request_id TEXT,
    raw TEXT NOT NULL,
    accepted TEXT,
    status TEXT NOT NULL DEFAULT 'processing',
    evidence TEXT NOT NULL DEFAULT '{}',
    task_ids TEXT NOT NULL DEFAULT '[]',
    resolution TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, request_id)
);
CREATE INDEX IF NOT EXISTS speech_capture_user ON speech_captures(user_id, status);
CREATE TABLE IF NOT EXISTS siri_outbox (
    event_id TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    raw TEXT NOT NULL,
    response TEXT NOT NULL,
    next_part INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS siri_messages (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    capture_id TEXT NOT NULL,
    PRIMARY KEY (channel_id, message_id)
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
    due_date: str | None = None


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "due_date" not in {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
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
                    bool(row["done"]), row["notion_page_id"], row["due_date"])

    def add_task(self, user_id: int, content: str, category: str, due_date: str | None = None) -> Task:
        with self.transaction():
            cur = self.db.execute(
                "INSERT INTO tasks (user_id, content, category, due_date) VALUES (?, ?, ?, ?)",
                (user_id, content, category, due_date),
            )
            self.mark_pending(cur.lastrowid)
            if due_date:
                self.db.execute("INSERT INTO notion_date_sync VALUES (?, ?)",
                                (cur.lastrowid, self.pending_revision(cur.lastrowid)))
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

    def set_date(self, task_id: int, due_date: str | None) -> None:
        with self.transaction():
            self.db.execute("UPDATE tasks SET due_date = ? WHERE id = ?", (due_date, task_id))
            self.mark_pending(task_id)
            self.db.execute("INSERT INTO notion_date_sync VALUES (?, ?) ON CONFLICT(task_id) "
                            "DO UPDATE SET revision = excluded.revision",
                            (task_id, self.pending_revision(task_id)))

    def delete(self, task_id: int) -> None:
        with self.transaction():
            task = self.get(task_id)
            if task:
                self.db.execute("INSERT OR IGNORE INTO notion_deletions VALUES (?, ?)",
                                (self.source_id(task_id), task.notion_page_id))
            self.db.execute("DELETE FROM notion_sync WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM notion_title_sync WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM notion_date_sync WHERE task_id = ?", (task_id,))
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

    def teach_speech(self, user_id: int, original: str, intended: str,
                     category: str | None = None, context: str = "", kind: str = "vocabulary") -> int:
        if kind not in {"vocabulary", "example", "pattern", "category"}:
            raise ValueError("Unknown memory kind")
        if not original.strip() or not intended.strip() or max(len(original), len(intended), len(context)) > 4000:
            raise ValueError("Memory text must contain 1–4000 characters")
        existing = self.db.execute("""SELECT id FROM speech_memory WHERE user_id = ? AND kind = ?
            AND original = ? AND intended = ? AND category IS ? AND context = ? AND active = 1""",
            (user_id, kind, original.strip(), intended.strip(), category, context.strip())).fetchone()
        if existing:
            return existing[0]
        cur = self.db.execute("""INSERT INTO speech_memory
            (user_id, kind, original, intended, category, context) VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, kind, original.strip(), intended.strip(), category, context.strip()))
        self._commit()
        return cur.lastrowid

    def speech_memories(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM speech_memory WHERE user_id = ? AND active = 1 ORDER BY id DESC", (user_id,))]

    def disable_speech_memory(self, user_id: int, memory_id: int) -> bool:
        cur = self.db.execute("UPDATE speech_memory SET active = 0 WHERE id = ? AND user_id = ? AND active = 1",
                              (memory_id, user_id))
        self._commit()
        return bool(cur.rowcount)

    def begin_speech_capture(self, user_id: int, request_id: str | None, raw: str) -> dict:
        existing = self.db.execute("SELECT * FROM speech_captures WHERE user_id = ? AND request_id = ?",
                                   (user_id, request_id)).fetchone() if request_id else None
        if existing:
            if existing["raw"] != raw:
                raise ValueError("request_id was already used for different text")
            return dict(existing)
        capture_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO speech_captures (id, user_id, request_id, raw) VALUES (?, ?, ?, ?)",
                        (capture_id, user_id, request_id, raw))
        self._commit()
        return self.speech_capture(user_id, capture_id)

    def speech_capture(self, user_id: int, capture_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM speech_captures WHERE id = ? AND user_id = ?",
                              (capture_id, user_id)).fetchone()
        return dict(row) if row else None

    def finish_speech_capture(self, capture_id: str, accepted: str, status: str, evidence: dict,
                              task_ids: list[int]) -> None:
        self.db.execute("UPDATE speech_captures SET accepted = ?, status = ?, evidence = ?, task_ids = ? WHERE id = ?",
                        (accepted, status, json.dumps(evidence), json.dumps(task_ids), capture_id))
        self._commit()

    def speech_pending(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT * FROM speech_captures
            WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 20""", (user_id,))]

    def speech_history(self, user_id: int, limit: int = 500) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT id, content, category, done FROM
            (SELECT *, ROW_NUMBER() OVER (PARTITION BY category ORDER BY id DESC) AS position
             FROM tasks WHERE user_id = ?) WHERE position <= 60 ORDER BY position, id DESC LIMIT ?""",
            (user_id, limit))]

    def set_content(self, task_id: int, content: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE tasks SET content = ? WHERE id = ?", (content, task_id))
            self.mark_pending(task_id)
            self.db.execute("""INSERT INTO notion_title_sync VALUES (?, ?)
                ON CONFLICT(task_id) DO UPDATE SET revision = excluded.revision""",
                (task_id, self.pending_revision(task_id)))

    def enqueue_siri(self, event_id: str, channel_id: int, user_id: int, raw: str, response: str) -> None:
        self.db.execute("""INSERT OR IGNORE INTO siri_outbox
            (event_id, channel_id, user_id, raw, response) VALUES (?, ?, ?, ?, ?)""",
            (event_id, channel_id, user_id, raw, response))
        self._commit()

    def pending_siri(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("""SELECT * FROM siri_outbox
            WHERE sent_at IS NULL ORDER BY created_at, rowid LIMIT 20""")]

    def link_siri_message(self, channel_id: int, message_id: int, capture_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO siri_messages VALUES (?, ?, ?)",
                        (channel_id, message_id, capture_id.removeprefix("resolved:")))
        self._commit()

    def message_capture(self, channel_id: int, message_id: int) -> str | None:
        row = self.db.execute("SELECT capture_id FROM siri_messages WHERE channel_id = ? AND message_id = ?",
                              (channel_id, message_id)).fetchone()
        return row[0] if row else None

    def advance_siri(self, event_id: str, part: int, done: bool) -> None:
        self.db.execute("""UPDATE siri_outbox SET next_part = ?,
            sent_at = CASE WHEN ? THEN datetime('now') ELSE NULL END WHERE event_id = ?""",
            (part, int(done), event_id))
        self._commit()
