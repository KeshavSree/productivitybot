from charles.parser import parse, split_tasks


def test_splits_lines_bullets_and_semicolons():
    text = "- one\n* two\n1. three\n[ ] four\n* [x] five; six\n\n"
    assert split_tasks(text) == ["one", "two", "three", "four", "five", "six"]


def test_strips_filler_but_keeps_task():
    assert [t.content for t in parse("I need to finish 211 lab")] == ["Finish 211 lab"]
    assert [t.content for t in parse("remind me to email prof")] == ["Email prof"]
    assert [t.content for t in parse("todo: gotta call recruiter")] == ["Call recruiter"]


def test_keyword_task_keeps_full_content():
    [t] = parse("plan for stack marketing meeting")
    assert t.content == "Plan for stack marketing meeting"
    assert t.explicit_category is None


def test_explicit_tags():
    cases = {
        "stack: send investor update": ("Send investor update", "Stack"),
        "[cs 373] read chapter 2": ("Read chapter 2", "CS 373"),
        "#mlp write up results": ("Write up results", "MLP"),
        "finish homework #cs211": ("Finish homework", "CS 211"),
        "recruiting: email jane": ("Email jane", "Recruiting"),
    }
    for text, (content, cat) in cases.items():
        [t] = parse(text)
        assert (t.content, t.explicit_category.name) == (content, cat), text


def test_colon_that_is_not_a_category_is_kept():
    [t] = parse("meeting at 3:30 with team")
    assert t.content == "Meeting at 3:30 with team"
    [t] = parse("note: buy milk")
    assert t.content == "Note: buy milk" and t.explicit_category is None
