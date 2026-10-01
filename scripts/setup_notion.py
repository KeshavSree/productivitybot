"""Create the task board inside a Notion page you've shared with your integration.

Usage:
    python -m scripts.setup_notion "https://www.notion.so/My-Page-1a2b3c..."

Prints the NOTION_DATABASE_ID to put in your .env / Railway variables.
"""

import asyncio
import os
import re
import sys

from dotenv import load_dotenv

from charles.notion import Notion


def page_id_from(url_or_id: str) -> str:
    m = re.search(r"([0-9a-f]{32})(?:[?#].*)?$", url_or_id.replace("-", ""))
    if not m:
        raise SystemExit(f"Couldn't find a page ID in {url_or_id!r}")
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


async def main() -> None:
    load_dotenv()
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise SystemExit("NOTION_TOKEN is not set (put it in .env).")
    notion = Notion(token)
    try:
        db_id = await notion.create_board(page_id_from(sys.argv[1]))
    finally:
        await notion.close()
    print("Created the Tasks board.")
    print(f"NOTION_DATABASE_ID={db_id}")
    print("In Notion, switch the view to Board and group by Category to get colored columns.")


if __name__ == "__main__":
    asyncio.run(main())
