"""Builds the Discord components offline to check they're valid (row/ID limits)."""

from charles.bot import added_view, list_view, added_summary, control_command
from charles.core import TaskService
from charles.store import Store


async def test_views_build_and_respect_limits():
    svc = TaskService(Store(":memory:"), None)
    text = "\n".join(["buy milk", "call mom", "pay rent", "get haircut"] + [f"cs 373 hw {i}" for i in range(40)])
    added = await svc.add_from_text(1, text)
    view = added_view(added)
    comps = view.to_components()
    assert len(comps) <= 5
    for row in comps:
        for c in row["components"]:
            assert len(c["custom_id"]) <= 100
            assert len(c.get("options", [])) <= 25
    msg = added_summary(added)
    assert len(msg) <= 2000 and "⬜ **Inbox** (pick a category below)" in msg

    lv = list_view(svc.store.open_tasks(1))
    assert len(lv.to_components()) == 2
    assert list_view([]) is None


def test_trigger_word():
    from charles.bot import task_text
    bot = 999
    assert task_text("c do cs 373 hw", bot, "c") == "do cs 373 hw"
    assert task_text("C: apply to citadel", bot, "c") == "apply to citadel"
    assert task_text("c, stack standup\ncs 211 lab", bot, "c") == "stack standup\ncs 211 lab"
    assert task_text("<@999> grind leetcode", bot, "c") == "grind leetcode"
    assert task_text("cool, see you later", bot, "c") is None
    assert task_text("cs 373 hw", bot, "c") is None
    assert task_text("c", bot, "c") is None
    assert task_text("cs 373 hw", bot, "c", always=True) == "cs 373 hw"
    assert task_text("c cs 373 hw", bot, "c", always=True) == "cs 373 hw"


async def test_one_line_reply_and_no_menu_when_sorted():
    svc = TaskService(Store(":memory:"), None)
    added = await svc.add_from_text(1, "study for cs 373 exam")
    assert added_summary(added) == "Added **Study for cs 373 exam** to 🟦 **CS 373**"
    assert added_view(added) is None


def test_multi_mode_commands():
    assert control_command("c multi", "c") == "multi"
    assert control_command("C: Multi ", "c") == "multi"
    assert control_command("c end", "c") == "end"
    assert control_command("c stop", "c") == "end"
    assert control_command("c end of semester review", "c") is None
    assert control_command("multi", "c") is None


async def test_on_message_multi_flow():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    import discord
    from charles.bot import Charles

    bot = Charles(TaskService(Store(":memory:"), None), None, set(), None)
    bot._connection.user = MagicMock(id=999)  # what bot.user reads
    channel = MagicMock(spec=discord.TextChannel, id=5)

    def msg(text):
        return SimpleNamespace(content=text, author=SimpleNamespace(id=1, bot=False), channel=channel,
                               add_reaction=AsyncMock(), reply=AsyncMock())

    m = msg("random chat"); await bot.on_message(m); m.reply.assert_not_called()
    m = msg("c multi"); await bot.on_message(m); assert "Listening" in m.reply.call_args.args[0]
    m = msg("do stat 417 hw"); await bot.on_message(m)
    m.add_reaction.assert_called_with("👍")
    assert m.reply.call_args.args[0] == "Added **Do stat 417 hw** to 🟪 **STAT 417**"
    m = msg("c end"); await bot.on_message(m); assert m.reply.call_args.args[0] == "Stopped listening."
    m = msg("do interview"); await bot.on_message(m); m.reply.assert_not_called()
    m = msg("c do cs 373 hw"); await bot.on_message(m)
    assert m.reply.call_args.args[0] == "Added **Do cs 373 hw** to 🟦 **CS 373**"
    await bot.close()
