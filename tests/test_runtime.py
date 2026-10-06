"""Ensure the deployed entry point runs HTTP alongside Discord and closes resources."""

import asyncio
import sqlite3

import aiohttp
import pytest

import charles.__main__ as runtime


class FakeBot:
    instances = []

    def __init__(self, service, *args):
        self.service = service
        self.stop = asyncio.Event()
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def start(self, token):
        await self.stop.wait()

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


@pytest.fixture
def runtime_env(monkeypatch, tmp_path):
    FakeBot.instances = []
    monkeypatch.setattr(runtime, 'Charles', FakeBot)
    for name in ('NOTION_TOKEN', 'NOTION_DATABASE_ID', 'ALLOWED_USER_IDS', 'TASK_CHANNEL_ID', 'GUILD_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('DISCORD_TOKEN', 'fake-discord-token')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('PORT', '0')
    monkeypatch.setenv('CAPTURE_TOKEN', 'runtime-token-' + 'x' * 32)
    monkeypatch.setenv('CAPTURE_USER_ID', '42')


async def test_runtime_serves_capture_and_closes_database(runtime_env, monkeypatch):
    started = asyncio.Event()
    ports = []
    original = runtime.web.TCPSite
    class InstrumentedSite(original):
        async def start(self):
            await super().start()
            ports.append(self._server.sockets[0].getsockname()[1])
            started.set()
    monkeypatch.setattr(runtime.web, 'TCPSite', InstrumentedSite)
    task = asyncio.create_task(runtime.run())
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with aiohttp.ClientSession() as client:
            async with client.get(f'http://127.0.0.1:{ports[0]}/health') as response:
                assert response.status == 200 and (await response.json())['capture_enabled']
            async with client.post(f'http://127.0.0.1:{ports[0]}/capture',
                                   headers={'Authorization': 'Bearer runtime-token-' + 'x' * 32},
                                   json={'request_id': 'startup', 'text': 'stack meeting'}) as response:
                assert response.status == 200 and (await response.json())['action'] == 'added'
        FakeBot.instances[0].stop.set()
        await asyncio.wait_for(task, 5)
        assert FakeBot.instances[0].closed
        with pytest.raises(sqlite3.ProgrammingError):
            FakeBot.instances[0].service.store.open_tasks(42)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_discord_failure_still_closes_resources(runtime_env, monkeypatch):
    async def fail(self, token):
        raise RuntimeError('Discord login failed')
    monkeypatch.setattr(FakeBot, 'start', fail)
    with pytest.raises(RuntimeError, match='Discord login failed'):
        await runtime.run()
    assert FakeBot.instances[0].closed
    with pytest.raises(sqlite3.ProgrammingError):
        FakeBot.instances[0].service.store.open_tasks(42)
