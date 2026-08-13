from cogs import connections


def test_normalize_word_uppercases_and_strips():
    assert connections._normalize_word("  hello  ") == "HELLO"


def test_normalize_word_collapses_internal_whitespace():
    assert connections._normalize_word("multi   word   answer") == "MULTI WORD ANSWER"


def test_normalize_word_handles_newlines_and_tabs():
    assert connections._normalize_word("line\nbreak\tword") == "LINE BREAK WORD"
