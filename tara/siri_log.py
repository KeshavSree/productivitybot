"""Durable Discord transcript delivery, independent of Siri's response latency."""

import asyncio
import logging
import os

import discord

DEFAULT_CHANNEL_ID = 1559954374007070790
log = logging.getLogger(__name__)


def transcript_parts(raw: str, response: str, event_id: str) -> list[str]:
    label = discord.utils.escape_markdown(os.getenv("SIRI_AUTHOR_NAME", "Keshav"))[:80]
    content = (f"**{label}:**\n{discord.utils.escape_markdown(raw)}\n\n"
               f"**Tara:**\n{discord.utils.escape_markdown(response)}")
    return [content[i:i + 1850] for i in range(0, len(content), 1850)]


async def deliver_pending(bot, store) -> int:
    delivered = 0
    for row in store.pending_siri():
        try:
            channel = bot.get_channel(row["channel_id"]) or await bot.fetch_channel(row["channel_id"])
            parts = transcript_parts(row["raw"], row["response"], row["event_id"])
            for index in range(row["next_part"], len(parts)):
                sent = await channel.send(parts[index], allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
                with store.transaction():
                    store.link_siri_message(row["channel_id"], sent.id, row["event_id"])
                    store.advance_siri(row["event_id"], index + 1, index + 1 == len(parts))
            # A pending transcript may have been split using the older visible-ID format.
            store.advance_siri(row["event_id"], len(parts), True)
            delivered += 1
        except Exception:
            # A failed fetch/send leaves the outbox intact; capture never waits here.
            log.exception("Siri transcript delivery failed for capture %s", row["event_id"])
    return delivered


async def siri_worker(bot, service, interval: float = 15) -> None:
    await bot.wait_until_ready()
    while True:
        service.siri_requested.clear()
        await deliver_pending(bot, service.store)
        try:
            await asyncio.wait_for(service.siri_requested.wait(), interval)
        except asyncio.TimeoutError:
            pass
