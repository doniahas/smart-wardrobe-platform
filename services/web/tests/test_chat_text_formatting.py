"""Focused tests for the conservative Markdown-
artifact normalization fallback (services/chat_text_formatting.py)."""
from app.services.chat_text_formatting import normalize_chat_reply


def test_strips_double_asterisk_bold_but_keeps_the_text():
    assert normalize_chat_reply("**Style Recommendations**") == "Style Recommendations"


def test_strips_double_underscore_bold_but_keeps_the_text():
    assert normalize_chat_reply("Try __dark jeans__ with that.") == "Try dark jeans with that."


def test_strips_multiple_bold_spans_in_one_reply():
    text = "**Try a red top** with __dark jeans__ for a clean look."
    assert normalize_chat_reply(text) == "Try a red top with dark jeans for a clean look."


def test_strips_heading_prefix_at_line_start():
    assert normalize_chat_reply("# Style Recommendations") == "Style Recommendations"
    assert normalize_chat_reply("## Style Tips:") == "Style Tips:"


def test_strips_heading_and_bold_together_across_lines():
    text = "# Style Recommendations\n**Try a red top** with __dark jeans__."
    assert normalize_chat_reply(text) == "Style Recommendations\nTry a red top with dark jeans."


def test_does_not_touch_mid_sentence_hash_like_a_hashtag():
    assert normalize_chat_reply("This look is very #casual today.") == (
        "This look is very #casual today."
    )


def test_does_not_touch_a_lone_unpaired_double_asterisk():
    # Not a Markdown emphasis span (no closing pair) - left untouched rather
    # than guessed at.
    assert normalize_chat_reply("2 ** 3 is not what I meant") == "2 ** 3 is not what I meant"


def test_does_not_touch_ordinary_punctuation():
    text = "Wear it with confidence! It's a great choice - trust me."
    assert normalize_chat_reply(text) == text


def test_does_not_touch_numbered_lists_or_simple_bullets():
    text = "1. A red top\n2. Dark jeans\n- Add a belt\n- Add sneakers"
    assert normalize_chat_reply(text) == text


def test_does_not_touch_fenced_code_blocks():
    text = "Here is an example:\n```\ncolor: red\n```"
    assert normalize_chat_reply(text) == text


def test_empty_string_is_returned_unchanged():
    assert normalize_chat_reply("") == ""


def test_is_idempotent():
    text = "# Style Recommendations\n**Try a red top** with __dark jeans__."
    once = normalize_chat_reply(text)
    twice = normalize_chat_reply(once)
    assert once == twice


def test_plain_text_with_no_markdown_artifacts_is_unchanged():
    text = "Try a red top with dark jeans for a clean, casual look."
    assert normalize_chat_reply(text) == text
