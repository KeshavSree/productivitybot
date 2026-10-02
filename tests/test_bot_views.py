"""Builds the Discord components offline to check they're valid (row/ID limits)."""

from charles.bot import added_view, list_view, added_summary
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
    msg, embed = added_summary(added)
    assert "Inbox" in msg and len(embed.fields) == 2

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
