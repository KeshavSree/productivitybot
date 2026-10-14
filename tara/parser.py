"""Turn a free-form Discord message into a list of task strings.

Handles:
  - one task per line, bullets ("-", "*", "•", "1.", "[ ]"), and semicolons
  - filler at the start ("todo:", "I need to", "remind me to", "gotta", ...)
  - explicit category hints, which win over everything else:
      "stack: plan meeting", "[stack] plan meeting", "#stack plan meeting",
      "plan meeting #stack"
"""

import re
from dataclasses import dataclass

from .categories import Category, lookup

_BULLET = re.compile(r"^\s*(?:(?:[-*•·▪◦>]+|\d+[.)]|\[\s?[xX ]?\s?\])\s*)+")
_FILLER = re.compile(
    r"^(?:"
    r"to\s*-?\s*dos?\s*:?|tasks?\s*:|add\s*:|"
    r"(?:i\s+)?(?:need|have|want|got)\s+to|"
    r"(?:i\s+)?(?:should|must|gotta|gonna)|"
    r"(?:please\s+)?remind\s+me\s+to|"
    r"don'?t\s+forget\s+to|"
    r"remember\s+to"
    r")\s+",
    re.IGNORECASE,
)
_PREFIX = re.compile(r"^\s*(?:\[([^\]]{1,24})\]|#([\w ]{1,24}?)\b|([\w ]{1,24}?)\s*:)\s*(.+)$")
_SUFFIX_TAG = re.compile(r"^(.+?)\s+#(\w[\w ]{0,23})\s*$")


@dataclass
class ParsedTask:
    content: str
    explicit_category: Category | None = None


def split_tasks(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.splitlines():
        for piece in line.split(";"):
            piece = _BULLET.sub("", piece).strip()
            if piece:
                parts.append(piece)
    return parts


def _strip_filler(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = _FILLER.sub("", s).strip()
    return s


def _explicit(s: str) -> tuple[str, Category | None]:
    m = _PREFIX.match(s)
    if m:
        tag = next(g for g in m.groups()[:3] if g is not None)
        cat = lookup(tag)
        if cat:
            return m.group(4).strip(), cat
    m = _SUFFIX_TAG.match(s)
    if m:
        cat = lookup(m.group(2))
        if cat:
            return m.group(1).strip(), cat
    return s, None


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().rstrip(".,;")
    return s[:1].upper() + s[1:] if s else s


def parse(text: str) -> list[ParsedTask]:
    tasks = []
    for raw in split_tasks(text):
        s = _strip_filler(raw)
        s, cat = _explicit(s)
        s = _tidy(_strip_filler(s))
        if len(s) >= 2:
            tasks.append(ParsedTask(content=s, explicit_category=cat))
    return tasks
