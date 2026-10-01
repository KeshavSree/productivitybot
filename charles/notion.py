"""Minimal async Notion API client for the task board.

The board is a Notion database with three properties:
  Task (title), Category (select, one colored option per category), Done (checkbox).
Viewing it as a Board grouped by Category gives one colored column per category.
"""

import aiohttp

from .categories import CATEGORIES, INBOX

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

TITLE_PROP = "Task"
CATEGORY_PROP = "Category"
DONE_PROP = "Done"


class NotionError(RuntimeError):
    pass


def category_options() -> list[dict]:
    opts = [{"name": c.name, "color": c.color} for c in CATEGORIES]
    return opts + [{"name": INBOX, "color": "gray"}]


class Notion:
    def __init__(self, token: str, database_id: str | None = None, session: aiohttp.ClientSession | None = None):
        self.token = token
        self.database_id = database_id
        self._session = session

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": VERSION,
                "Content-Type": "application/json",
            })
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _req(self, method: str, path: str, json: dict | None = None) -> dict:
        http = await self._http()
        async with http.request(method, f"{API}{path}", json=json) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise NotionError(f"{resp.status} {data.get('code')}: {data.get('message')}")
            return data

    # ---- setup -----------------------------------------------------------------

    async def create_board(self, parent_page_id: str, title: str = "Tasks") -> str:
        data = await self._req("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "is_inline": True,
            "properties": {
                TITLE_PROP: {"title": {}},
                CATEGORY_PROP: {"select": {"options": category_options()}},
                DONE_PROP: {"checkbox": {}},
            },
        })
        self.database_id = data["id"]
        return data["id"]

    async def ensure_schema(self) -> None:
        """Add any category options that are missing (e.g. after editing categories.py)."""
        db = await self._req("GET", f"/databases/{self.database_id}")
        props = db["properties"]
        missing = [k for k in (TITLE_PROP, CATEGORY_PROP, DONE_PROP) if k not in props]
        if missing:
            raise NotionError(f"Database is missing properties {missing}; run scripts/setup_notion.py")
        existing = {o["name"] for o in props[CATEGORY_PROP]["select"]["options"]}
        new = [o for o in category_options() if o["name"] not in existing]
        if new:
            await self._req("PATCH", f"/databases/{self.database_id}", {
                "properties": {CATEGORY_PROP: {"select": {
                    "options": props[CATEGORY_PROP]["select"]["options"] + new}}},
            })

    # ---- tasks -----------------------------------------------------------------

    async def add_task(self, content: str, category: str) -> str:
        data = await self._req("POST", "/pages", {
            "parent": {"database_id": self.database_id},
            "properties": {
                TITLE_PROP: {"title": [{"text": {"content": content[:2000]}}]},
                CATEGORY_PROP: {"select": {"name": category}},
                DONE_PROP: {"checkbox": False},
            },
        })
        return data["id"]

    async def set_category(self, page_id: str, category: str) -> None:
        await self._req("PATCH", f"/pages/{page_id}", {
            "properties": {CATEGORY_PROP: {"select": {"name": category}}}})

    async def set_done(self, page_id: str, done: bool = True) -> None:
        await self._req("PATCH", f"/pages/{page_id}", {
            "properties": {DONE_PROP: {"checkbox": done}}})

    async def archive(self, page_id: str) -> None:
        await self._req("PATCH", f"/pages/{page_id}", {"archived": True})
