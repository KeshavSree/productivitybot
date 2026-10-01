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
