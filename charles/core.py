"""Shared task handling for Discord and the phone capture API."""

import asyncio
import logging
import re
import json
import weakref
import time
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz

from .classifier import Classifier, Result
from .notion import Notion
from .parser import parse, split_tasks
from .store import Store, Task

log = logging.getLogger(__name__)

# Similarity scores, not probabilities. Require both a strong title match and
# separation from the next candidate before changing a task automatically.
COMPLETION_SCORE_MIN = 88.0
COMPLETION_SCORE_MARGIN = 8.0

_CANCEL_PHRASES = (
    "no", "nope", "nah", "nothing", "never mind", "nevermind", "never mind that",
    "bye", "goodbye", "good bye", "bye bye", "cancel", "cancel that", "cancel it",
    "cancel this", "cancel capture", "cancel this capture", "stop", "stop listening",
    "forget it", "forget that", "forget about it", "scratch that", "ignore that",
    "no thanks", "no thank you", "nothing else", "nothing to add", "no task",
    "no tasks", "no more", "not now", "thats all", "that is all", "im good",
    "i am good", "im done", "i am done", "all good", "thanks", "thank you",
    "dont add anything", "do not add anything", "dont add that", "do not add that",
)
_CANCEL_PHRASE = "(?:" + "|".join(re.escape(p) for p in _CANCEL_PHRASES) + ")"
_CANCEL_UTTERANCE = re.compile(rf"{_CANCEL_PHRASE}(?: (?:and )?{_CANCEL_PHRASE})*")
_CANCEL_PREFIX = re.compile(r"^(?:(?:hey )?charles|actually|uh|um|oh|okay|ok|sorry|please) ")
_CANCEL_SUFFIX = re.compile(r" (?:thanks|thank you|please|charles)$")


@dataclass
class Added:
    task: Task
    result: Result


@dataclass
class TextResult:
    action: str
    message: str
    tasks: list[Task] = field(default_factory=list)
    added: list[Added] = field(default_factory=list)
    replayed: bool = False
    capture_id: str | None = None
    raw_text: str | None = None
    corrected_text: str | None = None
    correction_status: str | None = None

    @classmethod
    def from_receipt(cls, data: dict):
        return cls(data["action"], data["message"], [Task(**t) for t in data["tasks"]],
                   [Added(Task(**a["task"]), Result(**a["result"])) for a in data["added"]], True,
                   **{k: data.get(k) for k in ("capture_id", "raw_text", "corrected_text", "correction_status")})


class CaptureConflict(ValueError):
    """A capture ID was reused with different input."""


def completion_text(content: str) -> str | None:
    """Recognize completion statements, preserving 'complete X' as a new task."""
    text = content.strip().rstrip(".!? ")
    if re.search(r"\b(?:not|never|isn't|isnt|wasn't|wasnt)\b", text, re.I):
        return None
    patterns = (
        r"^(?P<task>.+?)\s+(?:(?:is|was|has\s+been)\s+)?(?:done|finished|complete|completed)$",
        r"^(?:I\s+(?:have\s+)?)?(?:finished|completed)\s+(?P<task>.+)$",
        r"^(?:I(?:'m| am)\s+)?done\s+with\s+(?P<task>.+)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if match:
            task = match.group("task").strip(" \t.,!?\"'“”‘’")
            return task or None
    return None


def normalize_task_name(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def cancellation_text(text: str) -> bool:
    """Recognize whole cancellation utterances, never words inside a task title."""
    text = normalize_task_name(re.sub(r"['’]", "", text))
    previous = None
    while text != previous:
        previous = text
        text = _CANCEL_PREFIX.sub("", text)
        text = _CANCEL_SUFFIX.sub("", text)
    return _CANCEL_UTTERANCE.fullmatch(text) is not None


def _spoken_name(text: str) -> str:
    # Separate numbers from words so CS373 and hw2 work like CS 373 and hw 2.
    text = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", text.casefold())
    words = normalize_task_name(text).split()
    if words and words[0] in {"do", "finish", "complete"}:
        words.pop(0)
    return " ".join({"hw": "homework", "hws": "homework"}.get(w, w) for w in words)


def matching_tasks(name: str, tasks: list[Task]) -> list[Task]:
    """Return one confident match, multiple close matches, or no safe match.

    Literal/normalized matches take precedence. Fuzzy comparisons use the entire
    title, with and without word ordering; a shared fragment alone isn't enough.
    Numeric tokens must agree, including their count, so omitted or mistyped
    assignment numbers never silently choose a different task.
    """
    exact = [t for t in tasks if normalize_task_name(t.content) == normalize_task_name(name)]
    if exact:
        return exact
    spoken = _spoken_name(name)
    normalized = [(t, _spoken_name(t.content)) for t in tasks]
    exact = [t for t, title in normalized if spoken and title == spoken]
    if exact:
        return exact
    # Tiny names have too little information for typo matching.
    if len(spoken.replace(" ", "")) < 4:
        return []
    numbers = sorted(re.findall(r"\d+", spoken))
    ranked = sorted(
        ((max(fuzz.ratio(spoken, title), fuzz.token_sort_ratio(spoken, title)), t)
         for t, title in normalized
         if title and sorted(re.findall(r"\d+", title)) == numbers),
        key=lambda item: (-item[0], item[1].id),
    )
    if not ranked or ranked[0][0] < COMPLETION_SCORE_MIN:
        return []
    best_score, best_task = ranked[0]
    if len(ranked) > 1 and best_score - ranked[1][0] < COMPLETION_SCORE_MARGIN:
        # Include close runners-up even below the score minimum: they still
        # make the leading candidate unsafe to complete without clarification.
        return [task for score, task in ranked if best_score - score < COMPLETION_SCORE_MARGIN]
    return [best_task]


class TaskService:
    def __init__(self, store: Store, notion: Notion | None):
        self.store = store
        self.notion = notion
        self.classifier = Classifier(store.keywords(), store.corrections())
        self._sync_lock = asyncio.Lock()
        self._schema_ready = False
        self.sync_requested = asyncio.Event()
        from .correction import CorrectionPipeline
        self.correction = CorrectionPipeline.from_env(store, self.classifier)
        self._capture_locks = weakref.WeakValueDictionary()

    def _add_local(self, user_id: int, text: str, category: str | None = None) -> list[Added]:
        added = []
        for parsed in parse(text):
            result = self.classifier.classify(parsed.content, parsed.explicit_category)
            if category and not parsed.explicit_category:
                result = Result(category, 0.0, "context", "Personal correction evidence; score is not a probability")
            task = self.store.add_task(user_id, parsed.content, result.category)
            added.append(Added(task, result))
        return added

    def _handle_local(self, user_id: int, text: str, category: str | None = None) -> TextResult:
        pieces = split_tasks(text)
        if cancellation_text(text) or (pieces and all(cancellation_text(piece) for piece in pieces)):
            return TextResult("cancelled", "Cancelled.")
        added, completed, actions, messages = [], [], [], []
        for piece in pieces:
            if cancellation_text(piece):
                continue
            name = completion_text(piece)
            if name:
                matches = matching_tasks(name, self.store.open_tasks(user_id))
                if len(matches) == 1:
                    self.store.set_done(matches[0].id)
                    task = self.store.get(matches[0].id)
                    completed.append(task)
                    actions.append("completed")
                    messages.append(f"Marked {task.content} complete.")
                elif matches:
                    actions.append("ambiguous")
                    choices = "; ".join(f'“{task.content[:120]}”' for task in matches[:3])
                    if len(matches) > 3:
                        choices += f"; and {len(matches) - 3} more"
                    messages.append(f"Multiple open tasks match {name}: {choices}. "
                                    "Please use the full task name, or use /list in Discord to choose one.")
                else:
                    actions.append("not_found")
                    messages.append(f"I couldn't find an open task named {name}.")
            else:
                for item in self._add_local(user_id, piece, category):
                    added.append(item)
                    actions.append("added")
                    messages.append(f"Added {item.task.content} to {item.task.category}.")
        action = actions[0] if actions and len(set(actions)) == 1 else "mixed" if actions else "empty"
        return TextResult(action, "\n".join(messages) or "I couldn't find any tasks in that.",
                          [a.task for a in added] + completed, added)

    async def handle_text(self, user_id: int, text: str, request_id: str | None = None,
                          *, sync: bool = True, speech: bool = False) -> TextResult:
        key = (user_id, request_id) if request_id else (user_id, object())
        lock = self._capture_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._handle_text(user_id, text, request_id, sync=sync, speech=speech)

    async def _handle_text(self, user_id: int, text: str, request_id: str | None,
                           *, sync: bool, speech: bool) -> TextResult:
        """Commit the task changes and receipt before performing any remote work.

        There is deliberately no await inside this transaction: both entrances share
        one event loop, so concurrent captures cannot create duplicate local tasks.
        """
        text = text.strip()
        receipt = self.store.capture_request(user_id, request_id) if request_id else None
        capture, decision = None, None
        if speech and not receipt:
            try:
                capture = self.store.begin_speech_capture(user_id, request_id, text)
            except ValueError as exc:
                raise CaptureConflict(str(exc)) from exc
            from .correction import Decision
            started = time.monotonic()
            try:
                decision = await asyncio.wait_for(self.correction.correct(user_id, text), timeout=7)
            except asyncio.TimeoutError:
                decision = Decision(text, "fallback", evidence={"failure": "capture_timeout"})
            decision.evidence["latency_ms"] = round((time.monotonic() - started) * 1000)
        with self.store.transaction():
            receipt = self.store.capture_request(user_id, request_id) if request_id else None
            if receipt:
                if receipt[0] != text:
                    raise CaptureConflict("request_id was already used for different text")
                result = TextResult.from_receipt(receipt[1])
            else:
                if decision and decision.status == "pending":
                    result = TextResult("pending", "I couldn't safely interpret that. Saved for clarification in Discord.")
                else:
                    result = self._handle_local(user_id, decision.text if decision else text,
                                                decision.category if decision else None)
                if capture:
                    result.capture_id = capture["id"]
                    result.raw_text = text
                    result.corrected_text = decision.text
                    result.correction_status = decision.status
                    self.store.finish_speech_capture(capture["id"], decision.text, decision.status,
                                                      decision.evidence, [t.id for t in result.tasks])
                if request_id:
                    self.store.save_capture_request(user_id, request_id, text, asdict(result))
        if result.tasks:
            self.sync_requested.set()
        if sync:
            for task in result.tasks:
                await self._push(task)
        # Refresh page IDs after sync, keeping the original outcome on a replay.
        for task in result.tasks:
            current = self.store.get(task.id)
            if current:
                task.notion_page_id = current.notion_page_id
        return result

    def sync_status(self, tasks: list[Task]) -> str:
        if not tasks:
            return "not_applicable"
        if not self.notion:
            return "local_only"
        return "pending" if any(self.store.pending_revision(t.id) is not None for t in tasks) else "synced"

    async def add_from_text(self, user_id: int, text: str) -> list[Added]:
        # Explicit /add remains an add action, even for a title containing "finished".
        with self.store.transaction():
            added = self._add_local(user_id, text)
        self.sync_requested.set()
        for item in added:
            await self._push(item.task)
            item.task = self.store.get(item.task.id)
        return added

    async def _sync_task(self, task_id: int) -> bool:
        """Called under the sync lock; newer local revisions remain queued."""
        task = self.store.get(task_id)
        revision = self.store.pending_revision(task_id)
        if not self.notion or not task or revision is None:
            return False
        try:
            if not self._schema_ready:
                await self.notion.ensure_schema()
                self._schema_ready = True
            if not task.notion_page_id:
                source_id = self.store.source_id(task.id)
                page = await self.notion.find_task(source_id)
                if not page:
                    page = await self.notion.add_task(task.content, task.category, source_id)
                self.store.set_notion_page(task.id, page)
                task.notion_page_id = page
            await self.notion.update_task(task.notion_page_id, task.category, task.done)
            title_revision = self.store.db.execute("SELECT revision FROM notion_title_sync WHERE task_id = ?",
                                                    (task_id,)).fetchone()
            if title_revision and title_revision[0] <= revision:
                await self.notion.set_content(task.notion_page_id, task.content)
                self.store.db.execute("DELETE FROM notion_title_sync WHERE task_id = ? AND revision = ?",
                                      (task_id, title_revision[0]))
                self.store._commit()
            self.store.acknowledge(task.id, revision)
            return True
        except Exception:
            log.exception("Notion sync failed for task %s; kept queued", task_id)
            return False

    async def _push(self, task: Task) -> None:
        async with self._sync_lock:
            await self._sync_task(task.id)

    async def _sync_deletions(self) -> int:
        if not self.notion:
            return 0
        count = 0
        rows = self.store.db.execute("SELECT source_id, page_id FROM notion_deletions").fetchall()
        for row in rows:
            try:
                if not self._schema_ready:
                    await self.notion.ensure_schema()
                    self._schema_ready = True
                page_id = row["page_id"] or await self.notion.find_task(row["source_id"])
                if page_id:
                    await self.notion.archive(page_id)
                with self.store.transaction():
                    self.store.db.execute("DELETE FROM notion_deletions WHERE source_id = ?", (row["source_id"],))
                count += 1
            except Exception:
                log.exception("Notion archive failed; kept queued")
        return count

    async def sync_pending(self) -> int:
        rows = self.store.db.execute("SELECT task_id FROM notion_sync ORDER BY task_id").fetchall()
        count = 0
        # Release between tasks so a Discord interaction can also sync promptly.
        for row in rows:
            async with self._sync_lock:
                count += await self._sync_task(row["task_id"])
        async with self._sync_lock:
            count += await self._sync_deletions()
        return count

    async def move(self, task_id: int, category: str, learn: bool = True) -> Task | None:
        task = self.store.get(task_id)
        if not task:
            return None
        with self.store.transaction():
            self.store.set_category(task_id, category)
            if learn:
                self.store.add_correction(task.content, category)
                self.store.teach_speech(task.user_id, task.content, task.content, category, kind="category")
        if learn:
            self.classifier.add_correction(task.content, category)
        self.sync_requested.set()
        await self._push(task)
        return self.store.get(task_id)

    async def complete(self, task_id: int) -> Task | None:
        task = self.store.get(task_id)
        if not task:
            return None
        self.store.set_done(task_id)
        self.sync_requested.set()
        await self._push(task)
        return self.store.get(task_id)

    async def delete(self, task_id: int) -> None:
        # Wait for any in-flight creation so its page can be durably archived.
        async with self._sync_lock:
            self.store.delete(task_id)
            self.sync_requested.set()
            await self._sync_deletions()

    def teach_keyword(self, category: str, keyword: str) -> None:
        self.store.add_keyword(category, keyword)
        self.classifier.add_keyword(category, keyword.lower())

    async def resolve_capture(self, user_id: int, capture_id: str, intended: str) -> TextResult:
        """A human verdict executes a pending capture once, without another model call."""
        intended = intended.strip()
        if not intended or len(intended) > 4000:
            raise ValueError("Use 1–4000 characters")
        with self.store.transaction():
            capture = self.store.speech_capture(user_id, capture_id)
            if not capture:
                raise ValueError("Capture not found")
            if capture["resolution"]:
                return TextResult.from_receipt(json.loads(capture["resolution"]))
            if capture["status"] != "pending":
                raise ValueError("Only pending captures can be resolved; use /correct for an existing task")
            result = self._handle_local(user_id, intended)
            result.capture_id, result.raw_text = capture_id, capture["raw"]
            result.corrected_text, result.correction_status = intended, "confirmed"
            self.store.teach_speech(user_id, capture["raw"], intended,
                result.tasks[0].category if len(result.tasks) == 1 else None, kind="example")
            self.store.finish_speech_capture(capture_id, intended, "resolved",
                {"method": "human"}, [t.id for t in result.tasks])
            self.store.db.execute("UPDATE speech_captures SET resolution = ? WHERE id = ?",
                                  (json.dumps(asdict(result)), capture_id))
        if result.tasks:
            self.sync_requested.set()
        return result

    async def correct_task(self, user_id: int, task_id: int, intended: str) -> Task:
        intended = intended.strip()
        task = self.store.get(task_id)
        if not task or task.user_id != user_id:
            raise ValueError("Task not found")
        if not intended or len(intended) > 2000 or len(split_tasks(intended)) != 1:
            raise ValueError("Supply one task title, 1–2000 characters")
        with self.store.transaction():
            self.store.set_content(task_id, intended)
            self.store.teach_speech(user_id, task.content, intended, task.category, kind="example")
            for capture in self.store.db.execute("""SELECT * FROM speech_captures
                WHERE user_id = ? ORDER BY created_at DESC LIMIT 200""", (user_id,)).fetchall():
                if json.loads(capture["task_ids"]) == [task_id] and not completion_text(capture["raw"]):
                    self.store.teach_speech(user_id, capture["raw"], intended, task.category, kind="example")
                    evidence = json.loads(capture["evidence"])
                    evidence.setdefault("feedback", []).append({"task_id": task_id, "intended": intended,
                                                                 "method": "human_title_correction"})
                    self.store.db.execute("UPDATE speech_captures SET evidence = ? WHERE id = ?",
                                          (json.dumps(evidence), capture["id"]))
                    break
        self.sync_requested.set()
        await self._push(task)
        return self.store.get(task_id)
