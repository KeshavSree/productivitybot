"""Conservative calendar-date recognition; task wording stays intact."""

import calendar
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MONTHS = {name.casefold(): i for i in range(1, 13)
          for name in (calendar.month_name[i], calendar.month_abbr[i])}
MONTH = "(?:" + "|".join(MONTHS) + ")"
WEEKDAYS = {name.casefold(): i for i, name in enumerate(calendar.day_name)}
WEEKDAY = "(?:" + "|".join(WEEKDAYS) + ")"
DAY = r"\d{1,2}(?:st|nd|rd|th)?"
# Bare course/assignment numbers never count as dates.
PATTERN = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{4}})?(?![\d/])|"
    rf"{MONTH}\.?\s+{DAY}(?:,?\s+\d{{4}})?|"
    rf"{DAY}\s+(?:of\s+)?{MONTH}\.?(?:\s+\d{{4}})?|"
    rf"(?:day\s+after\s+tomorrow|today|tomorrow|yesterday)|"
    rf"in\s+\d+\s+(?:days?|weeks?)|"
    rf"(?:next|this|on|by|due(?:\s+on)?)\s+{WEEKDAY})\b", re.I)


def local_today() -> date:
    return datetime.now(ZoneInfo(os.getenv("TASK_TIMEZONE", "America/Indiana/Indianapolis"))).date()


def _calendar_day(month: int, day: int, year: int | None, today: date) -> date | None:
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    # A date without a year means its next occurrence, including leap days.
    for candidate_year in range(today.year, min(today.year + 5, 10000)):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _resolve(phrase: str, today: date) -> date | None:
    text = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", phrase.casefold())
    text = " ".join(re.sub(r"[.,]", "", text).split())
    relative = {"today": 0, "tomorrow": 1, "day after tomorrow": 2, "yesterday": -1}
    if text in relative:
        return today + timedelta(days=relative[text])
    match = re.fullmatch(r"in\s+(\d+)\s+(days?|weeks?)", text)
    if match:
        days = int(match[1]) * (7 if match[2].startswith("week") else 1)
        try:
            return today + timedelta(days=days)
        except OverflowError:
            return None
    if text.split()[-1] in WEEKDAYS:
        delta = (WEEKDAYS[text.split()[-1]] - today.weekday()) % 7
        # 'next Friday' is the next Friday strictly after today.
        return today + timedelta(days=delta or (7 if text.startswith("next ") else 0))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    if "/" in text:
        numbers = list(map(int, text.split("/")))
        return _calendar_day(numbers[0], numbers[1], numbers[2] if len(numbers) == 3 else None, today)
    words = text.replace(" of ", " ").split()
    if words[0] in MONTHS:
        month, day = MONTHS[words[0]], int(words[1])
    else:
        day, month = int(words[0]), MONTHS[words[1]]
    return _calendar_day(month, day, int(words[2]) if len(words) == 3 else None, today)


def date_mentions(text: str) -> tuple[str, ...]:
    """Literal date phrases also serve as immutable speech-correction evidence."""
    return tuple(m[0].casefold() for m in PATTERN.finditer(text))


def extract_date(text: str, today: date | None = None) -> str | None:
    """One calendar day in ISO format; conflicting/invalid dates stay unset.

    Numeric dates use US month/day order. Times and ranges are not inferred.
    """
    matches = list(PATTERN.finditer(text))
    if not matches:
        return None
    for match in matches:
        before, after = text[max(0, match.start() - 25):match.start()], text[match.end():]
        if re.search(r"\b(?:not|except|instead\s+of)\s+(?:on\s+|by\s+)?$", before, re.I):
            return None
        if re.match(r"\s*(?:[-–]\s*\d|(?:to|through|until|or)\b)", after, re.I):
            return None
    reference = today or local_today()
    resolved = [_resolve(match[0], reference) for match in matches]
    if None in resolved or len(set(resolved)) != 1:
        return None
    return resolved[0].isoformat()
