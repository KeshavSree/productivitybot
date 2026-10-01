import pytest

from charles.categories import INBOX
from charles.classifier import Classifier
from charles.parser import parse


@pytest.fixture(scope="module")
def clf():
    return Classifier()


def classify(clf, text):
    [t] = parse(text)
    return clf.classify(t.content, t.explicit_category)


@pytest.mark.parametrize("text,expected", [
    ("plan for stack marketing meeting", "Stack"),
    ("cs373 hw 2", "CS 373"),
    ("CS 373 project proposal", "CS 373"),
    ("cs-37300 quiz", "CS 373"),
    ("finish 211 lab", "CS 211"),
    ("Comp Sci 211 exam review", "CS 211"),
    ("study for the 417 midterm", "STAT 417"),
    ("stats417 pset", "STAT 417"),
    ("STAT 417 hw", "STAT 417"),
    ("work on MLP slides", "MLP"),
    ("prep for google interview", "Recruiting"),
    ("grind leetcode", "Recruiting"),
    ("apply to jane street", "Recruiting"),
    ("update resume", "Recruiting"),
    ("coffee chat with sarah from meta", "Recruiting"),
    ("buy groceries", INBOX),
    ("call mom", INBOX),
])
def test_categories(clf, text, expected):
    assert classify(clf, text).category == expected


def test_numbers_that_are_not_courses(clf):
    assert classify(clf, "pay $373 rent").category != "CS 373"
    assert classify(clf, "call at 2:11").category != "CS 211"
    assert classify(clf, "room 4170").category != "STAT 417"


def test_unclear_goes_to_inbox_not_a_guess(clf):
    r = classify(clf, "email professor about homework")
    assert r.category == INBOX


def test_corrections_teach_the_model():
    clf = Classifier()
    assert classify(clf, "read about bayesian estimators").category == INBOX
    for t in ["read about maximum likelihood estimators", "estimators practice problems",
              "bayesian estimators notes"]:
        clf.add_correction(t, "STAT 417")
    assert classify(clf, "review estimators chapter").category == "STAT 417"


def test_learned_keyword_wins():
    clf = Classifier(learned_keywords={"Stack": ["figma"]})
    assert classify(clf, "make figma mockups").category == "Stack"


def test_explicit_tag_beats_rules(clf):
    assert classify(clf, "stack: interview a designer").category == "Stack"
