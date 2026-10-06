"""Shared task handling for Discord and the phone capture API."""

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field

from .classifier import Classifier, Result
from .notion import Notion
from .parser import parse, split_tasks
from .store import Store, Task

log = logging.getLogger(__name__)


@dataclass
class Added:
    task: Task
    result: Result


@dataclass
class TextResult:
    action: str
    message: str
    tasks: list[Task] = field(default_factory=list)
    added: list[Added] = field(default_factory=list)
    replayed: bool = False

    @classmethod
    def from_receipt(cls, data: dict):
        return cls(data["action"], data["message"], [Task(**t) for t in data["tasks"]],
                   [Added(Task(**a["task"]), Result(**a["result"])) for a in data["added"]], True)


class CaptureConflict(ValueError):
    """A capture ID was reused with different input."""


def completion_text(content: str) -> str | None:
    """Recognize completion statements, preserving 'complete X' as a new task."""
    text = content.strip().rstrip(".!? ")
    patterns = (
        r"^(?P<task>.+?)\s+(?:(?:is|was|has\s+been)\s+)?(?:done|finished|complete|completed)$",
        r"^(?:I\s+(?:have\s+)?)?(?:finished|completed)\s+(?P<task>.+)$",
        r"^(?:I(?:'m| am)\s+)?done\s+with\s+(?P<task>.+)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if match:
            task = match.group("task").strip(" \t.,!?\"'“”‘’")
            return task or None
    return None


def normalize_task_name(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def _spoken_name(text: str) -> str:
    # Only predictable variations: never fuzzy-match a different assignment/number.
    text = re.sub(r"\b(cs|stat)\s*(\d+)\b", r"\1 \2", text.casefold())
    words = normalize_task_name(text).split()
    if words and words[0] in {"do", "finish", "complete"}:
        words.pop(0)
    return " ".join({"hw": "homework", "hws": "homework"}.get(w, w) for w in words)


def matching_tasks(name: str, tasks: list[Task]) -> list[Task]:
    exact = [t for t in tasks if normalize_task_name(t.content) == normalize_task_name(name)]
    if exact:
        return exact
    spoken = _spoken_name(name)
    return [t for t in tasks if spoken and _spoken_name(t.content) == spoken]


class TaskService:
    def __init__(self, store: Store, notion: Notion | None):
        self.store = store
        self.notion = notion
        self.classifier = Classifier(store.keywords(), store.corrections())
        self._sync_lock = asyncio.Lock()
        self._schema_ready = False
        self.sync_requested = asyncio.Event()

    def _add_local(self, user_id: int, text: str) -> list[Added]:
        added = []
        for parsed in parse(text):
            result = self.classifier.classify(parsed.content, parsed.explicit_category)
            task = self.store.add_task(user_id, parsed.content, result.category)
            added.append(Added(task, result))
        return added

    def _handle_local(self, user_id: int, text: str) -> TextResult:
        added, completed, actions, messages = [], [], [], []
        for piece in split_tasks(text):
            name = completion_text(piece)
            if name:
                matches = matching_tasks(name, self.store.open_tasks(user_id))
                if len(matches) == 1:
                    self.store.set_done(matches[0].id)
                    task = self.store.get(matches[0].id)
                    completed.append(task)
                    actions.append("completed")
                    messages.append(f"Marked {task.content} complete.")
                elif matches:
                    actions.append("ambiguous")
                    messages.append(f"Multiple open tasks match {name}. Use /list in Discord to choose one.")
                else:
                    actions.append("not_found")
                    messages.append(f"I couldn't find an open task named {name}.")
            else:
                for item in self._add_local(user_id, piece):
                    added.append(item)
                    actions.append("added")
                    messages.append(f"Added {item.task.content} to {item.task.category}.")
        action = actions[0] if actions and len(set(actions)) == 1 else "mixed" if actions else "empty"
        return TextResult(action, "\n".join(messages) or "I couldn't find any tasks in that.",
                          [a.task for a in added] + completed, added)

    async def handle_text(self, user_id: int, text: str, request_id: str | None = None,
                          *, sync: bool = True) -> TextResult:
        """Commit the task changes and receipt before performing any remote work.

        There is deliberately no await inside this transaction: both entrances share
        one event loop, so concurrent captures cannot create duplicate local tasks.
        """
        text = text.strip()
        with self.store.transaction():
            receipt = self.store.capture_request(user_id, request_id) if request_id else None
            if receipt:
                if receipt[0] != text:
                    raise CaptureConflict("request_id was already used for different text")
                result = TextResult.from_receipt(receipt[1])
            else:
                result = self._handle_local(user_id, text)
                if request_id:
                    self.store.save_capture_request(user_id, request_id, text, asdict(result))
        self.sync_requested.set()
        if sync:
            for task in result.tasks:
                await self._push(task)
        # Refresh page IDs after sync, keeping the original outcome on a replay.
        for task in result.tasks:
            current = self.store.get(task.id)
            if current:
                task.notion_page_id = current.notion_page_id
        return result

    def sync_status(self, tasks: list[Task]) -> str:
        if not tasks:
            return "not_applicable"
        if not self.notion:
            return "local_only"
        return "pending" if any(self.store.pending_revision(t.id) is not None for t in tasks) else "synced"

    async def add_from_text(self, user_id: int, text: str) -> list[Added]:
        # Explicit /add remains an add action, even for a title containing "finished".
        with self.store.transaction():
            added = self._add_local(user_id, text)
        self.sync_requested.set()
        for item in added:
            await self._push(item.task)
            item.task = self.store.get(item.task.id)
        return added

    async def _sync_task(self, task_id: int) -> bool:
        """Called under the sync lock; newer local revisions remain queued."""
        task = self.store.get(task_id)
        revision = self.store.pending_revision(task_id)
        if not self.notion or not task or revision is None:
            return False
        try:
            if not self._schema_ready:
                await self.notion.ensure_schema()
                self._schema_ready = True
            if not task.notion_page_id:
                source_id = self.store.source_id(task.id)
                page = await self.notion.find_task(source_id)
                if not page:
                    page = await self.notion.add_task(task.content, task.category, source_id)
                self.store.set_notion_page(task.id, page)
                task.notion_page_id = page
            await self.notion.update_task(task.notion_page_id, task.category, task.done)
            self.store.acknowledge(task.id, revision)
            return True
        except Exception:
            log.exception("Notion sync failed for task %s; kept queued", task_id)
            return False

    async def _push(self, task: Task) -> None:
        async with self._sync_lock:
            await self._sync_task(task.id)

    async def _sync_deletions(self) -> int:
        if not self.notion:
            return 0
        count = 0
        rows = self.store.db.execute("SELECT source_id, page_id FROM notion_deletions").fetchall()
        for row in rows:
            try:
                if not self._schema_ready:
                    await self.notion.ensure_schema()
                    self._schema_ready = True
                page_id = row["page_id"] or await self.notion.find_task(row["source_id"])
                if page_id:
                    await self.notion.archive(page_id)
                with self.store.transaction():
                    self.store.db.execute("DELETE FROM notion_deletions WHERE source_id = ?", (row["source_id"],))
                count += 1
            except Exception:
                log.exception("Notion archive failed; kept queued")
        return count

    async def sync_pending(self) -> int:
        rows = self.store.db.execute("SELECT task_id FROM notion_sync ORDER BY task_id").fetchall()
        count = 0
        # Release between tasks so a Discord interaction can also sync promptly.
        for row in rows:
            async with self._sync_lock:
                count += await self._sync_task(row["task_id"])
        async with self._sync_lock:
            count += await self._sync_deletions()
        return count

    async def move(self, task_id: int, category: str, learn: bool = True) -> Task | None:
        task = self.store.get(task_id)
        if not task:
            return None
        with self.store.transaction():
            self.store.set_category(task_id, category)
            if learn:
                self.store.add_correction(task.content, category)
        if learn:
            self.classifier.add_correction(task.content, category)
        self.sync_requested.set()
        await self._push(task)
        return self.store.get(task_id)

    async def complete(self, task_id: int) -> Task | None:
        task = self.store.get(task_id)
        if not task:
            return None
        self.store.set_done(task_id)
        self.sync_requested.set()
        await self._push(task)
        return self.store.get(task_id)

    async def delete(self, task_id: int) -> None:
        # Wait for any in-flight creation so its page can be durably archived.
        async with self._sync_lock:
            self.store.delete(task_id)
            self.sync_requested.set()
            await self._sync_deletions()

    def teach_keyword(self, category: str, keyword: str) -> None:
        self.store.add_keyword(category, keyword)
        self.classifier.add_keyword(category, keyword.lower())
