"""Regression coverage for the [object Object] validation-message bug: a
FastAPI validation-error array (or any non-string detail) was being passed
straight to showMsg()/setChatBubbleText(), which coerces it via implicit
toString(). formatApiError() now sits between every d.detail read and its
display call. Source-text assertions only, matching this repo's existing
frontend-test convention (see test_logout_auth_state.py) - no JS execution
tooling exists in this project (see docs/testing.md's "Known gaps").
"""
import pytest


def _extract_function_body(js: str, function_name: str) -> str:
    """Returns the source of one top-level `function NAME() { ... }` by
    brace-matching from its opening `{` - same helper used in
    test_logout_auth_state.py."""
    start = js.index(f"function {function_name}(")
    brace_start = js.index("{", start)
    depth = 0
    i = brace_start
    while i < len(js):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[brace_start : i + 1]
        i += 1
    raise AssertionError(f"Could not find a matching closing brace for function {function_name!r}")


def test_format_api_error_is_defined(client):
    resp = client.get("/ui/app.js")
    assert resp.status_code == 200
    body = _extract_function_body(resp.text, "formatApiError")
    assert "typeof detail === 'string'" in body
    assert "Array.isArray(detail)" in body
    assert "entry.msg" in body
    assert "detail.message" in body
    # Never touches these fields, by construction.
    assert "loc" not in body
    assert ".input" not in body


@pytest.mark.parametrize(
    "js_file,fragment",
    [
        ("app.js", "showMsg('register-msg', formatApiError(d.detail, 'Registration failed.'), 'err')"),
        ("app.js", "showMsg('login-msg', formatApiError(d.detail, 'Login failed.'), 'err')"),
        ("app.js", "showMsg('add-msg', formatApiError(d.detail, 'Failed to add item.'), 'err')"),
        ("app.js", "showMsg('edit-msg', formatApiError(d.detail, 'Failed to update item.'), 'err')"),
        ("app.js", "showMsg('rec-msg', formatApiError(d.detail, 'Could not get a recommendation.'), 'err')"),
        ("app.js", "showMsg('rec-feedback-msg', formatApiError(d.detail, 'Could not record feedback.'), 'err')"),
        ("app.js", "showMsg('feedback-msg', formatApiError(d.detail, 'Failed to save rating.'), 'err')"),
        ("app.js", "showMsg('memory-msg', formatApiError(d.detail, 'Failed to save preferences.'), 'err')"),
        ("chat.js", "formatApiError(d.detail, 'Could not send your message. Please try again.')"),
        ("explore-messages.js", "showMsg('thread-msg', formatApiError(d.detail, 'Failed to send message.'), 'err')"),
        ("explore-posts.js", "showMsg('post-msg', formatApiError(d.detail, 'Failed to create post.'), 'err')"),
        ("explore-posts.js", "formatApiError(d.detail, 'Failed to add comment.')"),
        ("outfit-builder.js", "error: formatApiError(d.detail, 'Failed to save outfit.')"),
    ],
)
def test_every_known_call_site_routes_through_format_api_error(client, js_file, fragment):
    resp = client.get(f"/ui/{js_file}")
    assert resp.status_code == 200
    assert fragment in resp.text


def test_no_call_site_uses_raw_detail_fallback_anymore(client):
    """Global regression guard: the old, unwrapped `d.detail ||` pattern
    must not reappear anywhere in the served static JS."""
    for js_file in ("app.js", "chat.js", "explore-messages.js", "explore-posts.js", "outfit-builder.js"):
        resp = client.get(f"/ui/{js_file}")
        assert "d.detail ||" not in resp.text, f"{js_file} still uses raw d.detail"
