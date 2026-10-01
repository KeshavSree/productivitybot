"""Runs the service against a fake Notion API served locally by aiohttp."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import charles.notion as notion_mod
from charles.categories import INBOX
from charles.core import TaskService
from charles.notion import Notion
from charles.store import Store


class FakeNotion:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.db = None
        self.fail = False

    def app(self):
        app = web.Application()
        app.router.add_post("/v1/databases", self.create_db)
        app.router.add_get("/v1/databases/{id}", self.get_db)
        app.router.add_patch("/v1/databases/{id}", self.patch_db)
        app.router.add_post("/v1/pages", self.create_page)
        app.router.add_patch("/v1/pages/{id}", self.patch_page)
        return app

    async def create_db(self, req):
        assert req.headers["Authorization"] == "Bearer secret"
        self.db = await req.json()
        self.db["id"] = "db1"
        return web.json_response(self.db)

    async def get_db(self, req):
        return web.json_response(self.db)

    async def patch_db(self, req):
        body = await req.json()
        self.db["properties"].update(body["properties"])
        return web.json_response(self.db)

    async def create_page(self, req):
        if self.fail:
            return web.json_response({"code": "service_unavailable", "message": "down"}, status=503)
        body = await req.json()
        pid = f"page{len(self.pages) + 1}"
        self.pages[pid] = body
        return web.json_response({"id": pid})

    async def patch_page(self, req):
        body = await req.json()
        page = self.pages[req.match_info["id"]]
        if "archived" in body:
            page["archived"] = body["archived"]
        page["properties"].update(body.get("properties", {}))
        return web.json_response({"id": req.match_info["id"]})


@pytest.fixture
async def fake(monkeypatch):
    fake = FakeNotion()
    server = TestServer(fake.app())
    await server.start_server()
    monkeypatch.setattr(notion_mod, "API", str(server.make_url("/v1")))
    yield fake
    await server.close()


def cat(page):
    return page["properties"]["Category"]["select"]["name"]


async def test_board_setup_and_full_flow(fake):
    n = Notion("secret")
    db_id = await n.create_board("parent-page")
    assert db_id == "db1"
    opts = {o["name"]: o["color"] for o in fake.db["properties"]["Category"]["select"]["options"]}
    assert opts["Stack"] == "orange" and opts[INBOX] == "gray" and len(opts) == 7
    await n.ensure_schema()

    svc = TaskService(Store(":memory:"), n)
    added = await svc.add_from_text(42, "plan for stack marketing meeting\n- cs373 hw 2\n- buy groceries")
    assert [a.task.category for a in added] == ["Stack", "CS 373", INBOX]
    assert [cat(p) for p in fake.pages.values()] == ["Stack", "CS 373", INBOX]
    assert fake.pages["page1"]["properties"]["Task"]["title"][0]["text"]["content"] == "Plan for stack marketing meeting"

    # Fixing a category moves it in Notion and teaches the classifier.
    inbox_task = added[2].task
    await svc.move(inbox_task.id, "Stack")
    assert cat(fake.pages["page3"]) == "Stack"
    assert svc.store.corrections() == [("Buy groceries", "Stack")]

    await svc.complete(added[0].task.id)
    assert fake.pages["page1"]["properties"]["Done"]["checkbox"] is True
    assert [t.content for t in svc.store.open_tasks(42)] == ["Cs373 hw 2", "Buy groceries"]

    await svc.delete(added[1].task.id)
    assert fake.pages["page2"]["archived"] is True
    await n.close()


async def test_notion_outage_keeps_task_and_retries(fake):
    n = Notion("secret")
    await n.create_board("parent-page")
    svc = TaskService(Store(":memory:"), n)
    fake.fail = True
    [a] = await svc.add_from_text(1, "stack standup notes")
    assert a.task.notion_page_id is None and not fake.pages
    fake.fail = False
    assert await svc.sync_pending() == 1
    assert svc.store.get(a.task.id).notion_page_id == "page1"
    await n.close()


async def test_learning_survives_restart(tmp_path):
    path = tmp_path / "c.db"
    svc = TaskService(Store(path), None)
    svc.teach_keyword("STAT 417", "regression")
    svc2 = TaskService(Store(path), None)
    [a] = await svc2.add_from_text(1, "regression worksheet")
    assert a.task.category == "STAT 417"
