"""Run with: python -m charles"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .bot import Charles, env_ids, env_int
from .core import TaskService
from .notion import Notion
from .store import Store


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

    notion = None
    if os.getenv("NOTION_TOKEN") and os.getenv("NOTION_DATABASE_ID"):
        notion = Notion(os.environ["NOTION_TOKEN"], os.environ["NOTION_DATABASE_ID"])
    else:
        logging.warning("NOTION_TOKEN or NOTION_DATABASE_ID missing; tasks will only be saved locally")

    store = Store(Path(os.getenv("DATA_DIR", "./data")) / "charles.db")
    bot = Charles(TaskService(store, notion), env_int("TASK_CHANNEL_ID"), env_ids("ALLOWED_USER_IDS"), env_int("GUILD_ID"),
                  os.getenv("TRIGGER_WORD", "c").strip())
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
