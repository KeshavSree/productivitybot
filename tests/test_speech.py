"""Contract checks, not a claim of accuracy on unseen Siri speech."""

import asyncio
import json

import pytest

from tara.core import CaptureConflict, TaskService
from tara.correction import CorrectionPipeline, protected
from tara.retrieval import retrieve, sound, similarity
from tara.store import Store


class Model:
    model = "fake"

    def __init__(self, proposal=None, failure=None):
        self.proposal, self.failure, self.calls = proposal, failure, 0

    async def propose(self, raw, context):
        self.calls += 1
        await asyncio.sleep(0)
        if self.failure:
            raise self.failure
        return self.proposal


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("CORRECTION_MODE", "active")
    service = TaskService(Store(":memory:"), None)
    yield service
    service.store.close()


def proposal(original="urban", replacement="URBN", refs=None, category=""):
    return {"decision": "corrected", "category": category, "reason": "Known name",
            "edits": [{"original": original, "replacement": replacement, "evidence": refs or ["memory:1"]}]}


async def test_known_context_and_new_names(svc):
    svc.store.teach_speech(42, "urban", "URBN", context="video interview")
    captured = await svc.handle_text(42, "do urban video interview", "voice", speech=True, sync=False)
    assert captured.corrected_text == "do URBN video interview"
    assert captured.correction_status == "corrected"
    urban = await svc.handle_text(42, "urban planning", "planning", speech=True, sync=False)
    assert urban.corrected_text == "urban planning"
    foreign = await svc.handle_text(99, "do urban video interview", "voice", speech=True, sync=False)
    assert foreign.corrected_text == "do urban video interview"
    novel = await svc.handle_text(42, "call Zyravex", "novel", speech=True, sync=False)
    assert novel.corrected_text == "call Zyravex"
    typed = await svc.handle_text(42, "do urban video interview", sync=False)
    assert typed.tasks[0].content == "Do urban video interview"


async def test_category_rule_and_disabled_memory(svc):
    mid = svc.store.teach_speech(42, "urban", "URBN", "Stack")
    raw = "urban planning"
    assert (await svc.correction.correct(42, raw)).text == raw
    assert (await svc.correction.correct(42, "stack: urban interview")).text == "stack: URBN interview"
    assert not svc.store.disable_speech_memory(99, mid)
    assert svc.store.disable_speech_memory(42, mid)
    assert (await svc.correction.correct(42, "stack: urban interview")).text == "stack: urban interview"


async def test_conflicting_rules_stay_pending(svc):
    svc.store.teach_speech(42, "urban", "URBN")
    svc.store.teach_speech(42, "urban", "Urban Labs")
    result = await svc.handle_text(42, "urban interview", "conflict", speech=True, sync=False)
    assert result.action == "pending" and not svc.store.open_tasks(42)


async def test_two_compatible_dictionary_edits(svc):
    svc.store.teach_speech(42, "urban", "URBN")
    svc.store.teach_speech(42, "inner view", "interview")
    result = await svc.handle_text(42, "urban video inner view", "two-edits", speech=True, sync=False)
    assert result.corrected_text == "URBN video interview"
    assert result.action == "added"


@pytest.mark.parametrize(("raw", "intended"), [
    ("homework 2", "homework 3"), ("do not email Sam", "email Sam"),
    ("URBN interview", "URBN interview is done"), ("complete urban interview", "urban interview is complete"),
    ("buy milk", "buy milk; do laundry"), ("stack: urban interview", "mlp: URBN interview"),
])
async def test_protected_meaning(svc, raw, intended):
    svc.store.teach_speech(42, raw, intended)
    model = Model(proposal(raw, intended))
    svc.correction.model = model
    result = await svc.handle_text(42, raw, "protected", speech=True, sync=False)
    assert result.action == "pending"
    assert not svc.store.open_tasks(42)


async def test_supported_model_edits_and_completion(svc):
    svc.store.teach_speech(42, "urban", "URBN", context="video interview")
    task = svc.store.add_task(42, "Do URBN video interview", "Stack")
    svc.correction.model = Model(proposal())
    result = await svc.handle_text(42, "urban video interview is done", "done", speech=True, sync=False)
    assert result.action == "completed" and svc.store.get(task.id).done


async def test_chicago_completion_bypasses_unnecessary_model_review(svc):
    task = svc.store.add_task(42, "pay everyone from the chicago trip", "Inbox")
    other = svc.store.add_task(99, "pay everyone from the chicago trip", "Inbox")
    model = Model({"decision": "pending", "category": "", "edits": [], "reason": "Uncertain"})
    svc.correction.model = model
    result = await svc.handle_text(42, "Pay everyone from the Chicago trip is done", "chicago", speech=True, sync=False)
    assert result.action == "completed" and svc.store.get(task.id).done
    assert not svc.store.get(other.id).done
    assert model.calls == 0
    replay = await svc.handle_text(42, "Pay everyone from the Chicago trip is done", "chicago", speech=True, sync=False)
    assert replay.replayed and model.calls == 0


async def test_ambiguous_completion_still_requires_selection(svc):
    for _ in range(2):
        svc.store.add_task(42, "pay everyone from the chicago trip", "Inbox")
    result = await svc.handle_text(42, "Pay everyone from the Chicago trip is done", "two-trips", speech=True, sync=False)
    assert result.action == "ambiguous"
    assert len(svc.store.open_tasks(42)) == 2


async def test_unsupported_edits_and_bad_schema(svc):
    svc.correction.model = Model(proposal(refs=["memory:999"]))
    result = await svc.handle_text(42, "urban interview", "unsupported", speech=True, sync=False)
    assert result.action == "pending" and not result.tasks
    svc.correction.model = Model({"edits": "garbage"})
    fallback = await svc.handle_text(42, "buy milk", "bad-schema", speech=True, sync=False)
    assert fallback.correction_status == "fallback" and fallback.corrected_text == "buy milk"


async def test_concurrent_receipts_do_not_recall_model(svc):
    model = Model({"decision": "unchanged", "edits": [], "category": "", "reason": "Plausible"})
    svc.correction.model = model
    results = await asyncio.gather(*(svc.handle_text(42, "buy milk", "duplicate", speech=True, sync=False)
                                     for _ in range(5)))
    assert model.calls == 1 and sum(r.replayed for r in results) == 4
    assert len(svc.store.open_tasks(42)) == 1
    svc.store.teach_speech(42, "milk", "oat milk")
    replay = await svc.handle_text(42, "buy milk", "duplicate", speech=True, sync=False)
    assert replay.corrected_text == "buy milk" and model.calls == 1
    with pytest.raises(CaptureConflict):
        await svc.handle_text(42, "buy tea", "duplicate", speech=True, sync=False)


async def test_timeout_shadow_and_cancellation(svc):
    svc.correction.model = Model(failure=asyncio.TimeoutError())
    r = await svc.handle_text(42, "buy milk", "timeout", speech=True, sync=False)
    assert r.correction_status == "fallback" and len(r.tasks) == 1
    model = svc.correction.model
    calls = model.calls
    r = await svc.handle_text(42, "nothing nevermind", "cancel", speech=True, sync=False)
    assert r.action == "cancelled" and model.calls == calls
    svc.store.teach_speech(42, "urban", "URBN")
    svc.correction.mode = "shadow"
    r = await svc.handle_text(42, "urban interview", "shadow", speech=True, sync=False)
    assert r.corrected_text == "urban interview" and r.correction_status == "shadow"
    capture = svc.store.speech_capture(42, r.capture_id)
    assert json.loads(capture["evidence"])["proposed_text"] == "URBN interview"


async def test_resolve_once_and_learning_survives_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    svc = TaskService(Store(tmp_path / "tasks.db"), None)
    svc.correction.model = Model({"decision": "pending", "edits": [], "category": "", "reason": "Unclear"})
    pending = await svc.handle_text(42, "urban inner view", "uncertain", speech=True, sync=False)
    with pytest.raises(ValueError):
        await svc.resolve_capture(99, pending.capture_id, "URBN interview")
    resolved = await svc.resolve_capture(42, pending.capture_id, "URBN interview")
    replay = await svc.resolve_capture(42, pending.capture_id, "URBN interview")
    assert replay.replayed and len(svc.store.open_tasks(42)) == 1
    svc.store.close()
    svc = TaskService(Store(tmp_path / "tasks.db"), None)
    result = await svc.handle_text(42, "urban inner view", "next", speech=True, sync=False)
    assert result.corrected_text == "URBN interview"
    assert svc.store.speech_memories(42)
    svc.store.close()


async def test_correct_title_teaches_without_recompleting(svc):
    result = await svc.handle_text(42, "urban inner view", "added", speech=True, sync=False)
    task = await svc.correct_task(42, result.tasks[0].id, "URBN interview")
    assert task.content == "URBN interview" and not task.done
    assert svc.store.db.execute("SELECT task_id FROM notion_title_sync").fetchone()[0] == task.id
    with pytest.raises(ValueError):
        await svc.correct_task(99, task.id, "Other title")
    next_capture = await svc.handle_text(42, "urban inner view", "next", speech=True, sync=False)
    assert next_capture.corrected_text == "URBN interview"


def test_context_budget_and_phonetics(svc):
    for i in range(25):
        svc.store.teach_speech(42, "urban " + str(i), "URBN " + str(i), context="interview " + "x" * 1000)
    context = retrieve(svc.store, 42, "urban interview", svc.classifier)
    assert len(context["vocabulary"]) <= 10
    assert len(json.dumps(context, ensure_ascii=False).encode()) <= 7000
    assert similarity("inner view", "interview") >= 85
    assert sound("John") == sound("Jon")


async def test_model_can_fix_a_new_variation_using_history(svc):
    task = svc.store.add_task(42, "URBN video interview", "Stack")
    svc.correction.model = Model(proposal("inner view", "interview", [f"task:{task.id}"], "Stack"))
    result = await svc.handle_text(42, "URBN video inner view", "new-variation", speech=True, sync=False)
    assert result.corrected_text == "URBN video interview"
    assert result.tasks[0].category == "Stack"


async def test_negated_completion_and_model_disabled(svc):
    task = svc.store.add_task(42, "do URBN video interview", "Stack")
    r = await svc.handle_text(42, "URBN video interview is not done", "negated", speech=True, sync=False)
    assert r.action == "pending" and not svc.store.get(task.id).done
    svc.correction.mode = "off"
    svc.store.teach_speech(42, "urban", "URBN")
    r = await svc.handle_text(42, "urban interview", "off", speech=True, sync=False)
    assert r.corrected_text == "urban interview"


async def test_groq_request_is_structured_and_bounded(monkeypatch):
    from tara.correction import GroqCorrector
    import tara.correction as module
    seen = {}

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"decision": "unchanged", "category": "", "edits": [], "reason": "Plausible"})}}]}

    class Session:
        def __init__(self, **kwargs): seen.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            seen.update(kwargs)
            seen["url"] = url
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    result = await GroqCorrector("fake-key").propose("buy milk", {})
    assert result["decision"] == "unchanged"
    assert seen["timeout"].total == 5
    assert seen["json"]["response_format"]["json_schema"]["strict"]
    assert len(seen["json"]["messages"]) == 2


def test_category_patterns_are_confirmed_and_isolated(svc):
    svc.store.teach_speech(42, "x", "prepare weekly update", "Stack", kind="example")
    svc.store.teach_speech(42, "y", "send weekly update", "Stack", kind="example")
    svc.store.teach_speech(99, "z", "secret weekly update", "Stack", kind="example")
    context = retrieve(svc.store, 42, "weekly update", svc.classifier)
    assert any("weekly update" in p["recurring_phrases"] for p in context["category_patterns"])
    assert "secret" not in json.dumps(context)


@pytest.mark.parametrize("statement", [
    "My flight is booked now", "My flight's booked", "I've booked my flight",
    "I booked my flight", "booked the flight", "My flight has already been booked",
])
async def test_booked_flight_completes_existing_task(svc, statement):
    task = svc.store.add_task(42, "Book flight", "Inbox")
    svc.correction.model = Model(failure=RuntimeError("Model must not be needed"))
    result = await svc.handle_text(42, statement, "flight", speech=True, sync=False)
    assert result.action == "completed"
    assert svc.store.get(task.id).done
    assert result.tasks[0].id == task.id
    assert svc.correction.model.calls == 0
    assert len(svc.store.speech_history(42)) == 1


@pytest.mark.parametrize("statement", [
    "My flight isn't booked", "My flight is not booked yet", "I haven't booked my flight",
    "I need to book my flight", "I will book my flight", "Book flight",
])
async def test_unfinished_or_future_flight_is_not_completed(svc, statement):
    task = svc.store.add_task(42, "Book flight", "Inbox")
    await svc.handle_text(42, statement, "unfinished", speech=True, sync=False)
    assert not svc.store.get(task.id).done


async def test_completed_action_with_no_match_is_not_added(svc):
    result = await svc.handle_text(42, "My flight is booked now", "missing", speech=True, sync=False)
    assert result.action == "not_found"
    assert not svc.store.open_tasks(42)


async def test_completed_action_preserves_number_and_ambiguity(svc):
    one = svc.store.add_task(42, "Book flight 1", "Inbox")
    two = svc.store.add_task(42, "Book flight 2", "Inbox")
    result = await svc.handle_text(42, "My flight 2 is booked now", "number", speech=True, sync=False)
    assert result.action == "completed" and svc.store.get(two.id).done
    assert not svc.store.get(one.id).done
    svc.store.add_task(42, "Book flight 1", "Inbox")
    result = await svc.handle_text(42, "My flight 1 is booked now", "ambiguous", speech=True, sync=False)
    assert result.action == "ambiguous"
    assert not svc.store.get(one.id).done
