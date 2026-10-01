"""Logout-state bugfix: after clicking Sign Out, the auth (Create Account /
Sign In) section must appear AND every authenticated app container must be
hidden - not just #app, and not left visible because some teardown call
running after showAuth() re-shows one of them as a side effect.

Same style as test_frontend_ui_redesign.py - lightweight, served-content
assertions against the exact static files FastAPI serves under `/ui/`, not a
JS test run (see docs/testing.md's "Known gaps": no frontend/JS
test-execution tooling exists in this project).

Root cause this guards against: `showAuth()` used to hide only `#app`, and
`logout()` called `showAuth()` *before* `teardownExplore()` -
`teardownExplore()` itself ends with `showTab('dashboard')`, which un-hides
`#app` again as part of its normal "reset to the default tab" behavior,
silently undoing showAuth()'s own hide. The fix makes `showAuth()` the
authoritative, always-hide-everything "logged out" state setter, and makes
`logout()` call it last so nothing afterward can re-show anything it hid.
"""


def _extract_function_body(js: str, function_name: str) -> str:
    """Returns the source of one top-level `function NAME() { ... }` by
    brace-matching from its opening `{` - good enough for these simple,
    non-nested-string-literal functions, and avoids depending on exact
    formatting/line numbers that would make this test brittle."""
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


def test_show_auth_hides_every_top_level_authenticated_container(client):
    """showAuth() must hide #app, #explore-app, #messages-app, and
    #chat-app, unconditionally - not just #app - so no authenticated
    surface can be left visible underneath the auth form regardless of
    which tab was active when the user logged out."""
    resp = client.get("/ui/app.js")
    assert resp.status_code == 200
    js = resp.text

    show_auth = _extract_function_body(js, "showAuth")
    for container_id in ("app", "explore-app", "messages-app", "chat-app", "user-bar"):
        assert f"getElementById('{container_id}').classList.add('hidden')" in show_auth, (
            f"showAuth() must hide #{container_id}"
        )

    # And the auth-facing surfaces must actually be shown.
    assert "getElementById('hero-section').classList.remove('hidden')" in show_auth
    assert "getElementById('auth-section').classList.remove('hidden')" in show_auth

    # The account dropdown must not be left open underneath the auth form.
    assert "closeAccountMenu()" in show_auth


def test_logout_calls_show_auth_after_teardown_explore_not_before(client):
    """The exact ordering bug: teardownExplore() ends with
    showTab('dashboard'), which un-hides #app as a side effect of resetting
    to the default tab. showAuth() must run AFTER that, not before, so its
    own hides are the last word on what's visible - otherwise this
    regresses silently again even with showAuth() itself fixed."""
    resp = client.get("/ui/app.js")
    assert resp.status_code == 200
    js = resp.text

    logout_body = _extract_function_body(js, "logout")
    teardown_index = logout_body.index("teardownExplore()")
    show_auth_index = logout_body.index("showAuth()")
    assert teardown_index < show_auth_index, (
        "logout() must call teardownExplore() before showAuth(), not after - "
        "teardownExplore()'s own showTab('dashboard') call un-hides #app, "
        "which must not happen after showAuth() has already hidden it"
    )


def test_logout_still_clears_token_first(client):
    """Regression guard: the reordering fix must not have disturbed the
    existing, unrelated logout behavior of clearing the stored token before
    anything else."""
    resp = client.get("/ui/app.js")
    assert resp.status_code == 200
    js = resp.text

    logout_body = _extract_function_body(js, "logout")
    # Strip the enclosing braces `_extract_function_body` includes, so this
    # checks the first *statement*, not the opening `{`.
    inner = logout_body.strip().removeprefix("{").removesuffix("}").strip()
    assert inner.startswith("clearToken();")
