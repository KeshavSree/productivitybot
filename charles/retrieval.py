"""Bounded retrieval from personal corrections, vocabulary, and task history.

Phonetic similarity generates candidates; it is never permission to edit text.
"""

import json
import re
from collections import Counter
from functools import lru_cache

from rapidfuzz import fuzz

from .categories import CATEGORIES, INBOX


def words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def contains(phrase: str, text: str) -> bool:
    return bool(words(phrase)) and f" {words(phrase)} " in f" {words(text)} "


@lru_cache(maxsize=1)
def pronunciations():
    import cmudict
    return cmudict.dict()


@lru_cache(maxsize=8192)
def sound(text: str) -> str:
    lexicon = pronunciations()
    result = []
    for word in words(text).split():
        options = lexicon.get(word)
        # Unknown names remain searchable by letters and explicitly taught aliases.
        result.extend(re.sub(r"\d", "", p) for p in options[0]) if options else result.append(word)
    return " ".join(result)


def similarity(query: str, candidate: str) -> float:
    q, c = words(query), words(candidate)
    if not q or not c:
        return 0.0
    lexical = max(fuzz.ratio(q, c), fuzz.WRatio(q, c))
    phonetic = fuzz.WRatio(sound(query), sound(candidate))
    return max(lexical, phonetic)


def retrieve(store, user_id: int, raw: str, classifier) -> dict:
    memories = store.speech_memories(user_id)
    ranked = sorted(memories, key=lambda m: (-similarity(raw, m["original"]), -m["id"]))
    relevant = [m for m in ranked if similarity(raw, m["original"]) >= 55]
    vocabulary = [m for m in relevant if m["kind"] == "vocabulary"][:10]
    examples = [m for m in relevant if m["kind"] in {"example", "category"}][:5]
    patterns = [m for m in relevant if m["kind"] == "pattern"][:5]
    history = store.speech_history(user_id)
    history.sort(key=lambda t: (-similarity(raw, t["content"]), -t["id"]))
    tasks, seen = [], set()
    for task in history:
        key = (words(task["content"]), task["category"])
        if key not in seen and similarity(raw, task["content"]) >= 55:
            tasks.append(task)
            seen.add(key)
        if len(tasks) == 5:
            break
    category_votes = Counter()
    for m in relevant[:20]:
        if m["category"]:
            category_votes[m["category"]] += similarity(raw, m["original"]) / 100
    for task in tasks:
        category_votes[task["category"]] += similarity(raw, task["content"]) / 200
    categories = [c for c, _ in category_votes.most_common(3)]
    for category in classifier.best_guesses(raw, 3):
        if category not in categories:
            categories.append(category)
    categories = categories[:4]
    descriptions = [{"name": c.name, "examples": list(c.examples[:2])}
                    for c in CATEGORIES if c.name in categories]
    descriptions.append({"name": INBOX, "examples": ["Any new or unrelated task"]})
    language = []
    for category in categories:
        phrases = Counter()
        seen_texts = set()
        for memory in memories:
            if memory["category"] != category or memory["kind"] not in {"example", "category"}:
                continue
            text = words(memory["intended"])
            if text in seen_texts:
                continue
            seen_texts.add(text)
            tokens = text.split()
            phrases.update(" ".join(tokens[i:i + n]) for n in (2, 3) for i in range(len(tokens) - n + 1))
        common = [phrase for phrase, count in phrases.most_common() if count >= 2 and len(phrase) <= 120][:5]
        if common:
            language.append({"category": category, "recurring_phrases": common})
    context = {"vocabulary": [], "examples": [], "patterns": [], "tasks": [],
               "categories": descriptions, "category_patterns": language}
    # Cap UTF-8 bytes, rather than estimating tokens from English character counts.
    # Leaves room for raw input/system instructions and handles non-ASCII memories.
    for field, items in (("vocabulary", vocabulary), ("examples", examples),
                         ("patterns", patterns), ("tasks", tasks)):
        for item in items:
            if field == "tasks":
                record = {"ref": f"task:{item['id']}", "text": item["content"],
                          "category": item["category"], "confirmed": False}
            else:
                record = {"ref": f"memory:{item['id']}", "original": item["original"],
                          "intended": item["intended"], "category": item["category"],
                          "context": item["context"], "kind": item["kind"], "confirmed": True}
            context[field].append(record)
            if len(json.dumps(context, ensure_ascii=False).encode()) > 7000:
                context[field].pop()
    return context
