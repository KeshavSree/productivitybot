"""Charles: the Discord side. Messages and slash commands in, buttons and menus out."""

import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands

from .categories import BY_NAME, CATEGORIES, INBOX
from .core import Added, TaskService
from .store import Task

log = logging.getLogger(__name__)

EMOJI = {"blue": "🟦", "green": "🟩", "purple": "🟪", "orange": "🟧", "pink": "🩷", "red": "🟥", "gray": "⬜"}
ALL_CATEGORIES = [c.name for c in CATEGORIES] + [INBOX]
CATEGORY_CHOICES = [app_commands.Choice(name=n, value=n) for n in ALL_CATEGORIES]


def emoji(category: str) -> str:
    c = BY_NAME.get(category.lower())
    return EMOJI[c.color] if c else EMOJI["gray"]


def short(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def service(interaction: discord.Interaction) -> TaskService:
    return interaction.client.service  # type: ignore[attr-defined]


def grouped_embed(title: str, tasks: list[Task], footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    by_cat: dict[str, list[Task]] = {}
    for t in tasks:
        by_cat.setdefault(t.category, []).append(t)
    for cat in ALL_CATEGORIES:
        if cat in by_cat:
            lines = "\n".join(f"• {short(t.content, 200)}" for t in by_cat[cat])
            embed.add_field(name=f"{emoji(cat)} {cat}", value=lines[:1024], inline=False)
    if not tasks:
        embed.description = "Nothing here."
    if footer:
        embed.set_footer(text=footer)
    return embed


# ---- components (custom ids carry task ids, so they keep working after a restart) ----

async def _check(interaction: discord.Interaction) -> bool:
    if interaction.client.is_allowed(interaction.user):  # type: ignore[attr-defined]
        return True
    await interaction.response.send_message("Sorry, this is a private todo bot.", ephemeral=True)
    return False


def category_options(current: str | None = None) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=n, value=n, emoji=emoji(n), default=(n == current)) for n in ALL_CATEGORIES]


class CategorySelect(discord.ui.DynamicItem[discord.ui.Select], template=r"charles:cat:(?P<task>\d+)"):
    """Pick a category for one task. Picking teaches the classifier."""

    def __init__(self, task_id: int, placeholder: str = "Move to…", current: str | None = None):
        super().__init__(discord.ui.Select(
            custom_id=f"charles:cat:{task_id}", placeholder=placeholder,
            options=category_options(current)))
        self.task_id = task_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check(interaction)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["task"]))

    async def callback(self, interaction: discord.Interaction):
        task = await service(interaction).move(self.task_id, self.item.values[0])
        if not task:
            return await interaction.response.send_message("That task no longer exists.", ephemeral=True)
        await interaction.response.send_message(
            f"Moved **{short(task.content)}** to {emoji(task.category)} **{task.category}**. I'll remember that.",
            ephemeral=True)


class FixPicker(discord.ui.DynamicItem[discord.ui.Select], template=r"charles:fix:(?P<ids>[\d,]+)"):
    """Pick one of the listed tasks, then get a category menu for it."""

    def __init__(self, tasks: list[Task] | list[int], placeholder: str = "Wrong category? Pick a task to fix"):
        ids = [t.id if isinstance(t, Task) else t for t in tasks]
        opts = [discord.SelectOption(label=short(t.content), value=str(t.id), emoji=emoji(t.category))
                for t in tasks if isinstance(t, Task)] or [discord.SelectOption(label=str(i), value=str(i)) for i in ids]
        super().__init__(discord.ui.Select(
            custom_id=f"charles:fix:{','.join(map(str, ids))}", placeholder=placeholder, options=opts))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check(interaction)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls([int(i) for i in match["ids"].split(",")])

    async def callback(self, interaction: discord.Interaction):
        task = service(interaction).store.get(int(self.item.values[0]))
        if not task:
            return await interaction.response.send_message("That task no longer exists.", ephemeral=True)
        view = discord.ui.View(timeout=None)
        view.add_item(CategorySelect(task.id, f"Move “{short(task.content, 60)}” to…", task.category))
        await interaction.response.send_message(view=view, ephemeral=True)


class DoneSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"charles:done:(?P<ids>[\d,]+)"):
    """Tick off one or more tasks."""

    def __init__(self, tasks: list[Task] | list[int]):
        ids = [t.id if isinstance(t, Task) else t for t in tasks]
        opts = [discord.SelectOption(label=short(t.content), value=str(t.id), emoji=emoji(t.category))
                for t in tasks if isinstance(t, Task)] or [discord.SelectOption(label=str(i), value=str(i)) for i in ids]
        super().__init__(discord.ui.Select(
            custom_id=f"charles:done:{','.join(map(str, ids))}", placeholder="✅ Mark done…",
            min_values=1, max_values=len(opts), options=opts))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check(interaction)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls([int(i) for i in match["ids"].split(",")])

    async def callback(self, interaction: discord.Interaction):
        svc = service(interaction)
        names = []
        for v in self.item.values:
            t = await svc.complete(int(v))
            if t:
                names.append(f"~~{short(t.content)}~~")
        remaining = svc.store.open_tasks(interaction.user.id)
        await interaction.response.edit_message(
            embed=grouped_embed("Your open tasks", remaining, footer=f"Done: {len(names)}"),
            view=list_view(remaining))


class UndoButton(discord.ui.DynamicItem[discord.ui.Button], template=r"charles:undo:(?P<ids>[\d,]+)"):
    def __init__(self, ids: list[int]):
        super().__init__(discord.ui.Button(
            label="Undo", style=discord.ButtonStyle.secondary, emoji="↩️",
            custom_id=f"charles:undo:{','.join(map(str, ids))}"))
        self.ids = ids

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check(interaction)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls([int(i) for i in match["ids"].split(",")])

    async def callback(self, interaction: discord.Interaction):
        svc = service(interaction)
        for i in self.ids:
            await svc.delete(i)
        await interaction.response.edit_message(content="Undone, those tasks were removed.", embed=None, view=None)


def _fits(ids: list[int], prefix: str) -> list[int]:
    # custom_id is capped at 100 characters; keep as many ids as fit.
    out: list[int] = []
    for i in ids:
        if len(prefix) + len(",".join(map(str, out + [i]))) > 100:
            break
        out.append(i)
    return out


def added_view(added: list[Added]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    tasks = [a.task for a in added]
    inbox = [t for t in tasks if t.category == INBOX]
    rows_left = 5
    for t in inbox[:3]:
        view.add_item(CategorySelect(t.id, f"Where does “{short(t.content, 60)}” go?"))
        rows_left -= 1
    sorted_tasks = [t for t in tasks if t.category != INBOX]
    if sorted_tasks and rows_left > 1:
        keep = set(_fits([t.id for t in sorted_tasks[:25]], "charles:fix:"))
        view.add_item(FixPicker([t for t in sorted_tasks if t.id in keep]))
    ids = _fits([t.id for t in tasks], "charles:undo:")
    if ids:
        view.add_item(UndoButton(ids))
    return view


def list_view(tasks: list[Task]) -> discord.ui.View | None:
    if not tasks:
        return None
    view = discord.ui.View(timeout=None)
    keep = set(_fits([t.id for t in tasks[:25]], "charles:done:"))
    view.add_item(DoneSelect([t for t in tasks if t.id in keep]))
    keep = set(_fits([t.id for t in tasks[:25]], "charles:fix:"))
    view.add_item(FixPicker([t for t in tasks if t.id in keep], "Move a task to another category"))
    return view


def added_summary(added: list[Added]) -> tuple[str, discord.Embed]:
    tasks = [a.task for a in added]
    inbox = sum(t.category == INBOX for t in tasks)
    msg = f"Added {len(tasks)} task{'s' if len(tasks) != 1 else ''} to Notion."
    if inbox:
        msg += f" I wasn't sure about {inbox}, so {'it is' if inbox == 1 else 'they are'} in Inbox. Pick a category below and I'll learn from it."
    return msg, grouped_embed("Sorted", tasks)


class AddModal(discord.ui.Modal, title="Add tasks"):
    text = discord.ui.TextInput(
        label="One task per line", style=discord.TextStyle.paragraph, max_length=4000,
        placeholder="plan for stack marketing meeting\ncs 373 hw 3\napply to jane street")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        added = await service(interaction).add_from_text(interaction.user.id, self.text.value)
        if not added:
            return await interaction.followup.send("I couldn't find any tasks in that.")
        msg, embed = added_summary(added)
        await interaction.followup.send(msg, embed=embed, view=added_view(added))


# ---- the bot -------------------------------------------------------------------

class Charles(commands.Bot):
    def __init__(self, svc: TaskService, task_channel_id: int | None, allowed: set[int], guild_id: int | None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.service = svc
        self.task_channel_id = task_channel_id
        self.allowed = allowed
        self.guild_id = guild_id
        self.tree.interaction_check = self._allowed_interaction  # type: ignore[method-assign]

    def is_allowed(self, user: discord.abc.User) -> bool:
        return not self.allowed or user.id in self.allowed

    async def _allowed_interaction(self, interaction: discord.Interaction) -> bool:
        if self.is_allowed(interaction.user):
            return True
        await interaction.response.send_message("Sorry, this is a private todo bot.", ephemeral=True)
        return False

    async def setup_hook(self):
        self.add_dynamic_items(CategorySelect, FixPicker, DoneSelect, UndoButton)
        register_commands(self.tree)
        if self.service.notion:
            try:
                await self.service.notion.ensure_schema()
                n = await self.service.sync_pending()
                if n:
                    log.info("Retried %d tasks that hadn't reached Notion", n)
            except Exception:
                log.exception("Notion check failed; tasks will still be saved locally")
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.is_allowed(message.author):
            return
        in_dm = isinstance(message.channel, discord.DMChannel)
        in_task_channel = self.task_channel_id and message.channel.id == self.task_channel_id
        mentioned = self.user in message.mentions
        if not (in_dm or in_task_channel or mentioned):
            return
        text = re.sub(rf"<@!?{self.user.id}>", "", message.content).strip()
        if not text:
            return
        async with message.channel.typing():
            added = await self.service.add_from_text(message.author.id, text)
        if added:
            msg, embed = added_summary(added)
            await message.reply(msg, embed=embed, view=added_view(added), mention_author=False)


def register_commands(tree: app_commands.CommandTree):
    @tree.command(name="add", description="Add tasks; I'll sort them into categories")
    @app_commands.describe(text="Optional. Leave empty to open a box for several lines.")
    async def add(interaction: discord.Interaction, text: str | None = None):
        if not text:
            return await interaction.response.send_modal(AddModal())
        await interaction.response.defer(thinking=True)
        added = await service(interaction).add_from_text(interaction.user.id, text)
        if not added:
            return await interaction.followup.send("I couldn't find any tasks in that.")
        msg, embed = added_summary(added)
        await interaction.followup.send(msg, embed=embed, view=added_view(added))

    @tree.command(name="list", description="Show your open tasks, tick them off or move them")
    @app_commands.choices(category=CATEGORY_CHOICES)
    async def list_(interaction: discord.Interaction, category: app_commands.Choice[str] | None = None):
        tasks = service(interaction).store.open_tasks(interaction.user.id, category.value if category else None)
        footer = f"Showing the first 25 of {len(tasks)}" if len(tasks) > 25 else None
        view = list_view(tasks)
        kwargs = {"view": view} if view else {}
        await interaction.response.send_message(
            embed=grouped_embed("Your open tasks", tasks, footer), ephemeral=True, **kwargs)

    @tree.command(name="keyword", description="Teach me a word that always means a category")
    @app_commands.describe(word="e.g. 'regression' for STAT 417, or 'figma' for Stack")
    @app_commands.choices(category=[c for c in CATEGORY_CHOICES if c.value != INBOX])
    async def keyword(interaction: discord.Interaction, category: app_commands.Choice[str], word: str):
        service(interaction).teach_keyword(category.value, word.strip())
        await interaction.response.send_message(
            f"Got it. Tasks mentioning **{word.strip()}** now go to {emoji(category.value)} **{category.value}**.",
            ephemeral=True)

    @tree.command(name="categories", description="See the categories and the words I use to recognise them")
    async def categories(interaction: discord.Interaction):
        learned = service(interaction).store.keywords()
        embed = discord.Embed(title="Categories", color=discord.Color.blurple(),
                              description="Tag a task to force a category: `stack: ...`, `#mlp ...`, `[cs 211] ...`")
        for c in CATEGORIES:
            words = list(c.keywords) + learned.get(c.name, [])
            value = "Matched by name or course number."
            if words:
                value += "\nAlso: " + ", ".join(words[:40])
            embed.add_field(name=f"{emoji(c.name)} {c.name}", value=value[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def env_int(name: str) -> int | None:
    v = os.getenv(name, "").strip()
    return int(v) if v else None


def env_ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}

