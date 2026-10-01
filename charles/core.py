"""Everything the bot does, independent of Discord: parse, classify, store, sync to Notion."""

import logging
from dataclasses import dataclass

from .classifier import Classifier, Result
from .notion import Notion
from .parser import parse
from .store import Store, Task

log = logging.getLogger(__name__)


@dataclass
class Added:
    task: Task
    result: Result


class TaskService:
    def __init__(self, store: Store, notion: Notion | None):
        self.store = store
        self.notion = notion
        self.classifier = Classifier(store.keywords(), store.corrections())

    async def add_from_text(self, user_id: int, text: str) -> list[Added]:
        added = []
        for parsed in parse(text):
            result = self.classifier.classify(parsed.content, parsed.explicit_category)
            task = self.store.add_task(user_id, parsed.content, result.category)
            await self._push(task)
            added.append(Added(self.store.get(task.id), result))
            log.info("added %r -> %s (%s %s)", task.content, result.category, result.method, result.detail)
        return added

    async def _push(self, task: Task) -> None:
        if not self.notion or task.notion_page_id:
            return
        try:
            page = await self.notion.add_task(task.content, task.category)
            self.store.set_notion_page(task.id, page)
        except Exception:
            log.exception("Notion add failed for task %s; will retry on next start", task.id)

    async def sync_pending(self) -> int:
        """Push tasks that never made it to Notion (e.g. Notion was down)."""
        rows = self.store.db.execute(
            "SELECT id FROM tasks WHERE notion_page_id IS NULL AND done = 0").fetchall()
        for r in rows:
            await self._push(self.store.get(r["id"]))
        return len(rows)

    async def move(self, task_id: int, category: str, learn: bool = True) -> Task | None:
        task = self.store.get(task_id)
        if not task:
            return None
        self.store.set_category(task_id, category)
        if learn:
            self.store.add_correction(task.content, category)
            self.classifier.add_correction(task.content, category)
        if self.notion and task.notion_page_id:
            try:
                await self.notion.set_category(task.notion_page_id, category)
            except Exception:
                log.exception("Notion move failed for task %s", task_id)
        return self.store.get(task_id)

    async def complete(self, task_id: int) -> Task | None:
        task = self.store.get(task_id)
        if not task:
            return None
        self.store.set_done(task_id)
        if self.notion and task.notion_page_id:
            try:
                await self.notion.set_done(task.notion_page_id)
            except Exception:
                log.exception("Notion complete failed for task %s", task_id)
        return self.store.get(task_id)

    async def delete(self, task_id: int) -> None:
        task = self.store.get(task_id)
        if not task:
            return
        self.store.delete(task_id)
        if self.notion and task.notion_page_id:
            try:
                await self.notion.archive(task.notion_page_id)
            except Exception:
                log.exception("Notion archive failed for task %s", task_id)

    def teach_keyword(self, category: str, keyword: str) -> None:
        self.store.add_keyword(category, keyword)
        self.classifier.add_keyword(category, keyword.lower())
