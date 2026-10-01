"""Category definitions: names, Notion colors, and the rules that recognise them.

Edit this file to change categories. Each category has:
  - patterns: regexes that, if they match, are a strong signal (course codes, names)
  - keywords: words or phrases that are a weaker signal
  - examples: seed sentences used to train the fallback ML classifier

Keywords can also be added at runtime from Discord with /keyword, which are stored
in SQLite and merged with these.
"""

from dataclasses import dataclass, field

INBOX = "Inbox"


@dataclass(frozen=True)
class Category:
    name: str
    color: str  # Notion select color
    patterns: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    examples: tuple[str, ...] = field(default=(), repr=False)


def _course(prefixes: str, number: str) -> tuple[str, ...]:
    # Matches "cs 373", "CS373", "cs-373", "cs_373", "cs 37300", or a bare "373".
    return (
        rf"\b(?:{prefixes})\s*[-_]?\s*{number}(?:00)?\b",
        rf"(?<![\d.$/:]){number}(?![\d.%:/])",
    )


CATEGORIES: tuple[Category, ...] = (
    Category(
        name="CS 373",
        color="blue",
        patterns=_course("cs|comp\\s*sci|compsci|c\\.s\\.", "373"),
        examples=(
            "cs 373 homework",
            "373 hw 4",
            "finish 373 project",
            "study for cs373 midterm",
            "watch 373 lecture",
            "cs 373 quiz",
            "submit 373 assignment",
            "373 office hours",
        ),
    ),
    Category(
        name="CS 211",
        color="green",
        patterns=_course("cs|comp\\s*sci|compsci|c\\.s\\.", "211"),
        examples=(
            "cs 211 homework",
            "211 hw 3",
            "finish 211 project",
            "study for cs211 exam",
            "watch 211 lecture",
            "cs 211 lab",
            "submit 211 assignment",
            "211 recitation",
        ),
    ),
    Category(
        name="STAT 417",
        color="purple",
        patterns=_course("stat|stats|statistics", "417"),
        examples=(
            "stat 417 homework",
            "417 problem set",
            "study for stat417 exam",
            "stats 417 reading",
            "417 hw 5",
            "watch stat 417 lecture",
            "417 practice problems",
        ),
    ),
    Category(
        name="Stack",
        color="orange",
        patterns=(r"\bstack\b",),
        examples=(
            "plan for stack marketing meeting",
            "stack standup",
            "stack sprint planning",
            "email stack team",
            "update stack roadmap",
            "stack investor update",
            "review stack pull request",
        ),
    ),
    Category(
        name="MLP",
        color="pink",
        patterns=(r"\bmlp\b",),
        examples=(
            "mlp meeting",
            "work on mlp",
            "mlp experiments",
            "update mlp doc",
            "mlp sync",
        ),
    ),
    Category(
        name="Recruiting",
        color="red",
        patterns=(
            r"\b(?:recruit(?:ing|er|ers)?|internships?|interviews?|resumes?|"
            r"leetcode|neetcode|career\s*fair|coffee\s*chats?|referrals?|"
            r"cover\s*letters?|offer\s*letters?|online\s*assessments?|OA|"
            r"hirevue|handshake|superday|onsite|phone\s*screen|"
            r"behavioral|recruiter)\b",
        ),
        keywords=(
            "apply",
            "application",
            "applications",
            "job",
            "jobs",
            "linkedin",
            "networking",
            "network with",
            "hiring",
            "return offer",
            "new grad",
            "follow up with recruiter",
            "thank you email",
        ),
        examples=(
            "apply to google internship",
            "prep for amazon interview",
            "update resume",
            "do 3 leetcode problems",
            "coffee chat with jane from meta",
            "ask for referral at stripe",
            "finish online assessment for citadel",
            "career fair prep",
            "follow up with recruiter",
            "practice behavioral questions",
            "send thank you email after interview",
            "apply to 5 jobs on linkedin",
        ),
    ),
)

NAMES: tuple[str, ...] = tuple(c.name for c in CATEGORIES)
BY_NAME: dict[str, Category] = {c.name.lower(): c for c in CATEGORIES}


def lookup(name: str) -> Category | None:
    """Find a category by name, ignoring case and spaces ("cs373" -> CS 373)."""
    key = name.strip().lower()
    if key in BY_NAME:
        return BY_NAME[key]
    squashed = key.replace(" ", "")
    for c in CATEGORIES:
        if c.name.lower().replace(" ", "") == squashed:
            return c
    return None
