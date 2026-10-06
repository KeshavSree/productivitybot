"""Run Discord and the phone API together: python -m charles."""

import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from .api import CaptureConfig, create_app
from .bot import Charles, env_ids, env_int
from .core import TaskService
from .notion import Notion
from .store import Store

log = logging.getLogger(__name__)


async def sync_worker(service: TaskService, interval: float = 30):
    """Retry at startup, on local changes, and periodically after outages."""
    while True:
        service.sync_requested.clear()
        try:
            await service.sync_pending()
        except Exception:
            log.exception("Background sync failed; will retry")
        try:
            await asyncio.wait_for(service.sync_requested.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    config = CaptureConfig.from_env()
    notion = None
    if os.getenv("NOTION_TOKEN") and os.getenv("NOTION_DATABASE_ID"):
        notion = Notion(os.environ["NOTION_TOKEN"], os.environ["NOTION_DATABASE_ID"])
    else:
        log.warning("Notion is not configured; tasks will only be saved locally")
    store = Store(Path(os.getenv("DATA_DIR", "./data")) / "charles.db")
    service = TaskService(store, notion)
    bot = Charles(service, env_int("TASK_CHANNEL_ID"), env_ids("ALLOWED_USER_IDS"), env_int("GUILD_ID"),
                  os.getenv("TRIGGER_WORD", "c").strip())
    runner = web.AppRunner(create_app(service, config), access_log=None, shutdown_timeout=15)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = []
    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
        log.info("HTTP server ready; phone capture %s", "enabled" if config else "disabled")
        async with bot:
            bot_task = asyncio.create_task(bot.start(token), name="discord")
            sync_task = asyncio.create_task(sync_worker(service), name="notion-sync")
            stop_task = asyncio.create_task(stop.wait(), name="shutdown")
            tasks = [bot_task, sync_task, stop_task]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if bot_task in done:
                await bot_task  # Fail visibly on invalid Discord credentials/startup errors.
            if sync_task in done:
                await sync_task
    finally:
        # Drain captures before stopping sync or closing the shared database.
        await runner.cleanup()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not bot.is_closed():
            await bot.close()
        if notion:
            await notion.close()
        store.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
