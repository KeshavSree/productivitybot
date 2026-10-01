"""SQLite storage: tasks, the Notion page each one maps to, learned keywords, corrections."""

import sqlite3
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

    def _task(self, row) -> Task:
        return Task(row["id"], row["user_id"], row["content"], row["category"],
                    bool(row["done"]), row["notion_page_id"])

    def add_task(self, user_id: int, content: str, category: str) -> Task:
        cur = self.db.execute(
            "INSERT INTO tasks (user_id, content, category) VALUES (?, ?, ?)",
            (user_id, content, category),
        )
        self.db.commit()
        return self.get(cur.lastrowid)

    def get(self, task_id: int) -> Task | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def set_notion_page(self, task_id: int, page_id: str) -> None:
        self.db.execute("UPDATE tasks SET notion_page_id = ? WHERE id = ?", (page_id, task_id))
        self.db.commit()

    def set_category(self, task_id: int, category: str) -> None:
        self.db.execute("UPDATE tasks SET category = ? WHERE id = ?", (category, task_id))
        self.db.commit()

    def set_done(self, task_id: int, done: bool = True) -> None:
        self.db.execute("UPDATE tasks SET done = ? WHERE id = ?", (int(done), task_id))
        self.db.commit()

    def delete(self, task_id: int) -> None:
        self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.db.commit()

    def open_tasks(self, user_id: int, category: str | None = None) -> list[Task]:
        q, args = "SELECT * FROM tasks WHERE user_id = ? AND done = 0", [user_id]
        if category:
            q += " AND category = ?"
            args.append(category)
        q += " ORDER BY category, id"
        return [self._task(r) for r in self.db.execute(q, args)]

    def add_keyword(self, category: str, keyword: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO keywords VALUES (?, ?)", (category, keyword.lower()))
        self.db.commit()

    def keywords(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.db.execute("SELECT category, keyword FROM keywords ORDER BY keyword"):
            out.setdefault(r["category"], []).append(r["keyword"])
        return out

    def add_correction(self, text: str, category: str) -> None:
        self.db.execute("INSERT INTO corrections (text, category) VALUES (?, ?)", (text, category))
        self.db.commit()

    def corrections(self) -> list[tuple[str, str]]:
        return [(r["text"], r["category"]) for r in self.db.execute("SELECT text, category FROM corrections")]
