"""Capture contracts: authorization, atomic receipts, safe completion, and retries."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from charles.api import CaptureConfig, create_app
from charles.core import TaskService
from charles.store import Store

TOKEN = "test-phone-token-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    service = TaskService(Store(":memory:"), None)
    async with TestClient(TestServer(create_app(service, CaptureConfig(TOKEN, 42)))) as client:
        yield client, service
    service.store.close()


async def test_auth_validation_and_disabled_api(client):
    client, service = client
    assert (await client.get('/health')).status == 200
    payload = {"request_id": "capture-1", "text": "stack meeting"}
    assert (await client.post('/capture', json=payload)).status == 401
    assert (await client.post('/capture', json=payload, headers={"Authorization": "Bearer é"})).status == 401
    assert (await client.post('/capture', json={**payload, "user_id": 99}, headers=HEADERS)).status == 400
    for invalid in ({}, [], {"text": "   ", "request_id": "a"}, {"text": "x" * 4001, "request_id": "a"},
                    {"text": "stack meeting", "request_id": "../escape"}):
        assert (await client.post('/capture', json=invalid, headers=HEADERS)).status == 400
    assert (await client.post('/capture', data='{broken', headers={**HEADERS, 'Content-Type': 'application/json'})).status == 400
    assert (await client.post('/capture', data='x' * 20000, headers={**HEADERS, 'Content-Type': 'application/json'})).status == 413
    assert (await client.post('/capture', data='task', headers=HEADERS)).status == 415
    assert not service.store.open_tasks(42)
    async with TestClient(TestServer(create_app(service))) as disabled:
        assert (await disabled.post('/capture', json=payload)).status == 503


async def test_concurrent_duplicate_capture_and_conflict(client):
    client, service = client
    payload = {"request_id": "one-capture", "text": "do cs 373 hw"}
    responses = await asyncio.gather(*(client.post('/capture', json=payload, headers=HEADERS) for _ in range(5)))
    bodies = [await r.json() for r in responses]
    assert all(r.status == 200 for r in responses)
    assert sum(body['replayed'] for body in bodies) == 4
    assert len(service.store.open_tasks(42)) == 1
    assert all(body['tasks'][0]['category'] == 'CS 373' for body in bodies)
    assert bodies[0]['sync_status'] == 'local_only'
    assert not service.store.open_tasks(99)
    response = await client.post('/capture', json={**payload, "text": "another task"}, headers=HEADERS)
    assert response.status == 409
    assert len(service.store.open_tasks(42)) == 1


async def test_completion_variations_and_ambiguity(client):
    client, service = client
    service.store.add_task(99, 'Do cs 373 hw', 'CS 373')
    for i, phrase in enumerate(('CS373 homework is done!', 'CS373 homework finished',
                                'finished CS373 homework', 'CS373 homework is complete')):
        task = service.store.add_task(42, 'Do cs 373 hw', 'CS 373')
        response = await client.post('/capture', json={'request_id': f'done-{i}', 'text': phrase}, headers=HEADERS)
        result = await response.json()
        assert result['action'] == 'completed'
        assert service.store.get(task.id).done
    assert not service.store.get(1).done  # Another user's identical task stays open.
    response = await client.post('/capture', json={'request_id': 'imperative', 'text': 'complete cs 373 hw'}, headers=HEADERS)
    assert (await response.json())['action'] == 'added'
    assert not service.store.open_tasks(42)[0].done
    service.store.add_task(42, 'Complete cs 373 hw', 'CS 373')
    response = await client.post('/capture', json={'request_id': 'ambiguous', 'text': 'cs 373 homework is done'}, headers=HEADERS)
    assert (await response.json())['action'] == 'ambiguous'
    assert len(service.store.open_tasks(42)) == 2
    response = await client.post('/capture', json={'request_id': 'missing', 'text': 'cs 373 homework 9 is done'}, headers=HEADERS)
    assert (await response.json())['action'] == 'not_found'
    assert len(service.store.open_tasks(42)) == 2


async def test_receipt_survives_restart_and_transaction_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / 'charles.db'
    service = TaskService(Store(path), None)
    first = await service.handle_text(42, 'stack meeting; cs 373 hw', 'persisted', sync=False)
    source = service.store.source_id(first.tasks[0].id)
    service.store.close()
    service = TaskService(Store(path), None)
    replay = await service.handle_text(42, 'stack meeting; cs 373 hw', 'persisted', sync=False)
    assert replay.replayed and len(service.store.open_tasks(42)) == 2
    assert service.store.source_id(replay.tasks[0].id) == source
    def fail(*args):
        raise RuntimeError('receipt write failed')
    monkeypatch.setattr(service.store, 'save_capture_request', fail)
    with pytest.raises(RuntimeError):
        await service.handle_text(42, 'buy milk', 'rollback', sync=False)
    assert len(service.store.open_tasks(42)) == 2
    assert service.store.capture_request(42, 'rollback') is None
    service.store.close()


async def test_rate_limit(client):
    client, service = client
    for _ in range(30):
        assert (await client.post('/capture', json={'request_id': 'same', 'text': 'stack meeting'}, headers=HEADERS)).status == 200
    response = await client.post('/capture', json={'request_id': 'same', 'text': 'stack meeting'}, headers=HEADERS)
    assert response.status == 429 and response.headers['Retry-After'] == '60'
    assert len(service.store.open_tasks(42)) == 1


def test_environment_configuration(monkeypatch):
    for name in ('CAPTURE_TOKEN', 'CAPTURE_USER_ID', 'ALLOWED_USER_IDS'):
        monkeypatch.delenv(name, raising=False)
    assert CaptureConfig.from_env() is None
    monkeypatch.setenv('CAPTURE_TOKEN', TOKEN)
    with pytest.raises(ValueError):
        CaptureConfig.from_env()
    monkeypatch.setenv('CAPTURE_USER_ID', '42')
    monkeypatch.setenv('ALLOWED_USER_IDS', '99')
    with pytest.raises(ValueError):
        CaptureConfig.from_env()
    monkeypatch.setenv('ALLOWED_USER_IDS', '42,99')
    assert CaptureConfig.from_env().user_id == 42
    with pytest.raises(ValueError):
        CaptureConfig('short', 42)
