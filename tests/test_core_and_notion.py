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
        self.fail_patch = False
        self.lose_create_response = False

    def app(self):
        app = web.Application()
        app.router.add_post("/v1/databases", self.create_db)
        app.router.add_get("/v1/databases/{id}", self.get_db)
        app.router.add_patch("/v1/databases/{id}", self.patch_db)
        app.router.add_post("/v1/databases/{id}/query", self.query_db)
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

    async def query_db(self, req):
        body = await req.json()
        source_id = body["filter"]["rich_text"]["equals"]
        results = [{"id": pid} for pid, page in self.pages.items()
                   if page["properties"].get("Charles ID", {}).get("rich_text", [{}])[0]
                   .get("text", {}).get("content") == source_id and not page.get("archived")]
        return web.json_response({"results": results})

    async def create_page(self, req):
        if self.fail:
            return web.json_response({"code": "service_unavailable", "message": "down"}, status=503)
        body = await req.json()
        pid = f"page{len(self.pages) + 1}"
        self.pages[pid] = body
        if self.lose_create_response:
            self.lose_create_response = False
            return web.json_response({"code": "service_unavailable", "message": "response lost"}, status=503)
        return web.json_response({"id": pid})

    async def patch_page(self, req):
        if self.fail_patch:
            return web.json_response({"code": "service_unavailable", "message": "down"}, status=503)
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


async def test_api_to_notion_and_completion_retry(fake):
    from aiohttp.test_utils import TestClient
    from charles.api import CaptureConfig, create_app
    n = Notion('secret')
    await n.create_board('parent-page')
    svc = TaskService(Store(':memory:'), n)
    token = 'capture-secret-' + 'x' * 32
    headers = {'Authorization': f'Bearer {token}'}
    async with TestClient(TestServer(create_app(svc, CaptureConfig(token, 42)))) as client:
        response = await client.post('/capture', headers=headers,
                                     json={'request_id': 'add', 'text': 'do cs 373 hw'})
        result = await response.json()
        assert result['sync_status'] == 'pending' and not fake.pages
        assert await svc.sync_pending() == 1
        task_id = result['tasks'][0]['id']
        fake.fail_patch = True
        response = await client.post('/capture', headers=headers,
                                     json={'request_id': 'done', 'text': 'CS373 homework is complete'})
        assert (await response.json())['action'] == 'completed'
        assert await svc.sync_pending() == 0
        assert svc.store.get(task_id).done and svc.store.pending_revision(task_id)
        fake.fail_patch = False
        assert await svc.sync_pending() == 1
        assert fake.pages['page1']['properties']['Done']['checkbox'] is True
        response = await client.post('/capture', headers=headers,
                                     json={'request_id': 'done', 'text': 'CS373 homework is complete'})
        assert (await response.json())['sync_status'] == 'synced'
    await n.close()


async def test_lost_notion_creation_response_is_recovered(fake):
    n = Notion('secret')
    await n.create_board('parent-page')
    svc = TaskService(Store(':memory:'), n)
    fake.lose_create_response = True
    result = await svc.handle_text(42, 'stack standup', 'capture')
    assert len(fake.pages) == 1 and svc.store.pending_revision(result.tasks[0].id)
    assert await svc.sync_pending() == 1
    assert len(fake.pages) == 1
    assert svc.store.get(result.tasks[0].id).notion_page_id == 'page1'
    await n.close()


async def test_creation_and_completion_survive_restart(fake, tmp_path):
    path = tmp_path / 'queue.db'
    n = Notion('secret')
    await n.create_board('parent-page')
    svc = TaskService(Store(path), n)
    await svc.handle_text(42, 'cs 211 hw', 'add', sync=False)
    result = await svc.handle_text(42, 'cs 211 homework finished', 'done', sync=False)
    svc.store.close()
    svc = TaskService(Store(path), n)
    assert await svc.sync_pending() == 1
    assert fake.pages['page1']['properties']['Done']['checkbox'] is True
    assert svc.sync_status(result.tasks) == 'synced'
    await n.close()


async def test_delete_recovers_page_after_lost_creation_response(fake):
    n = Notion('secret')
    await n.create_board('parent-page')
    svc = TaskService(Store(':memory:'), n)
    fake.lose_create_response = True
    [added] = await svc.add_from_text(42, 'stack standup')
    assert added.task.notion_page_id is None and len(fake.pages) == 1
    await svc.delete(added.task.id)
    assert fake.pages['page1']['archived'] is True
    assert not svc.store.open_tasks(42)
    await n.close()


async def test_new_local_revision_is_not_acknowledged_by_older_sync(fake, monkeypatch):
    import asyncio
    n = Notion('secret')
    await n.create_board('parent-page')
    svc = TaskService(Store(':memory:'), n)
    [added] = await svc.add_from_text(42, 'stack standup')
    svc.store.set_category(added.task.id, 'Inbox')
    entered, release = asyncio.Event(), asyncio.Event()
    update = n.update_task
    async def delayed(page_id, category, done):
        entered.set()
        await release.wait()
        await update(page_id, category, done)
    monkeypatch.setattr(n, 'update_task', delayed)
    syncing = asyncio.create_task(svc.sync_pending())
    await entered.wait()
    await svc.handle_text(42, 'stack standup is done', 'done', sync=False)
    release.set()
    await syncing
    assert svc.store.pending_revision(added.task.id) is not None
    assert fake.pages['page1']['properties']['Done']['checkbox'] is False
    await svc.sync_pending()
    assert svc.store.pending_revision(added.task.id) is None
    assert fake.pages['page1']['properties']['Done']['checkbox'] is True
    await n.close()
