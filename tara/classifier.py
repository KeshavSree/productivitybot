"""Decide which category a task belongs to. No LLM involved.

Order of decisions:
  1. An explicit tag in the message ("stack: ...", "#mlp") always wins.
  2. Rules: regex patterns (course codes, names) score 3, keywords score 1,
     keywords you taught it with /keyword score 2. A clear winner is used.
  3. Fallback ML: a TF-IDF + logistic regression model trained on the seed
     examples in categories.py plus every correction you make in Discord.
     It is only trusted when it is confident; otherwise the task goes to Inbox
     and the bot asks you to pick.
"""

import re
from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

from .categories import CATEGORIES, INBOX, Category, lookup

PATTERN_WEIGHT = 3
LEARNED_KEYWORD_WEIGHT = 2
KEYWORD_WEIGHT = 1

ML_MIN_PROB = 0.45
ML_MIN_MARGIN = 0.15
CORRECTION_REPEAT = 3  # corrections count more than seed examples

# Things that are clearly not any category, so the model has a "none of these" option.
_INBOX_EXAMPLES = (
    "buy groceries",
    "call mom",
    "do laundry",
    "pay rent",
    "go to the gym",
    "book dentist appointment",
    "clean my room",
    "pick up package",
    "renew passport",
    "get a haircut",
    "reply to emails",
    "buy birthday gift",
    "cancel subscription",
    "schedule doctor visit",
)


@dataclass
class Result:
    category: str
    confidence: float
    method: str  # "explicit" | "rules" | "ml" | "inbox"
    detail: str = ""

    @property
    def sure(self) -> bool:
        return self.category != INBOX


def _word_re(phrase: str) -> re.Pattern:
    return re.compile(r"\b" + r"\s+".join(map(re.escape, phrase.split())) + r"\b", re.IGNORECASE)


class Classifier:
    def __init__(self, learned_keywords: dict[str, list[str]] | None = None,
                 corrections: list[tuple[str, str]] | None = None):
        self._patterns = {
            c.name: [re.compile(p, re.IGNORECASE) for p in c.patterns] for c in CATEGORIES
        }
        self._keywords = {c.name: [_word_re(k) for k in c.keywords] for c in CATEGORIES}
        self._learned: dict[str, list[re.Pattern]] = {}
        for cat, words in (learned_keywords or {}).items():
            for w in words:
                self.add_keyword(cat, w)
        self._corrections = list(corrections or [])
        self._model: Pipeline | None = None
        self.retrain()

    # ---- rules -----------------------------------------------------------------

    def add_keyword(self, category: str, keyword: str) -> None:
        self._learned.setdefault(category, []).append(_word_re(keyword))

    def rule_scores(self, text: str) -> dict[str, int]:
        scores: dict[str, int] = {}
        for c in CATEGORIES:
            s = sum(PATTERN_WEIGHT for p in self._patterns[c.name] if p.search(text))
            s += sum(KEYWORD_WEIGHT for k in self._keywords[c.name] if k.search(text))
            s += sum(LEARNED_KEYWORD_WEIGHT for k in self._learned.get(c.name, []) if k.search(text))
            if s:
                scores[c.name] = s
        return scores

    # ---- ML --------------------------------------------------------------------

    def add_correction(self, text: str, category: str) -> None:
        self._corrections.append((text, category))
        self.retrain()

    def retrain(self) -> None:
        texts, labels = [], []
        for c in CATEGORIES:
            texts += c.examples
            labels += [c.name] * len(c.examples)
        texts += _INBOX_EXAMPLES
        labels += [INBOX] * len(_INBOX_EXAMPLES)
        for text, cat in self._corrections:
            texts += [text] * CORRECTION_REPEAT
            labels += [cat] * CORRECTION_REPEAT
        features = FeatureUnion([
            ("chars", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), lowercase=True, sublinear_tf=True)),
            ("words", TfidfVectorizer(analyzer="word", ngram_range=(1, 2), lowercase=True, sublinear_tf=True)),
        ])
        self._model = Pipeline([
            ("features", features),
            ("clf", LogisticRegression(C=10, max_iter=2000, class_weight="balanced")),
        ])
        self._model.fit(texts, labels)

    def ml_probs(self, text: str) -> list[tuple[str, float]]:
        probs = self._model.predict_proba([text])[0]
        return sorted(zip(self._model.classes_, probs), key=lambda x: -x[1])

    # ---- combined --------------------------------------------------------------

    def classify(self, text: str, explicit: Category | None = None) -> Result:
        if explicit:
            return Result(explicit.name, 1.0, "explicit")

        scores = self.rule_scores(text)
        if scores:
            ranked = sorted(scores.items(), key=lambda x: -x[1])
            top, top_score = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else 0
            if top_score > second:
                conf = min(1.0, 0.6 + 0.1 * (top_score - second))
                return Result(top, conf, "rules", f"scores={dict(ranked)}")
            # Tie between rule matches: let the model break it among the tied ones.
            tied = {name for name, s in ranked if s == top_score}
            for name, p in self.ml_probs(text):
                if name in tied:
                    return Result(name, p, "ml", f"tie-break among {sorted(tied)}")

        ranked = self.ml_probs(text)
        (top, p1), (_, p2) = ranked[0], ranked[1]
        if top != INBOX and p1 >= ML_MIN_PROB and p1 - p2 >= ML_MIN_MARGIN:
            return Result(top, float(p1), "ml", f"p={p1:.2f}")
        return Result(INBOX, float(p1), "inbox", f"best guess {top} p={p1:.2f}")

    def best_guesses(self, text: str, n: int = 3) -> list[str]:
        return [name for name, _ in self.ml_probs(text) if name != INBOX][:n]


def resolve_category(name: str) -> str | None:
    if name.strip().lower() == INBOX.lower():
        return INBOX
    c = lookup(name)
    return c.name if c else None
