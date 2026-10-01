"""New-conversation picker: lightweight content
assertions against the exact static files FastAPI serves under `/ui/` -
same style/scope as test_frontend_ui_redesign.py (not a JS test run, see
docs/testing.md's "Known gaps"). Proves the new "start a new conversation"
affordance actually exists in what's served, reuses the existing
startThreadWith flow instead of duplicating it, and that the pre-existing
Explore "Message" button path was left untouched.
"""


def test_new_conversation_ui_elements_exist(client):
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    html = resp.text
    for element_id in (
        "new-thread-toggle",
        "new-thread-panel",
        "new-thread-search",
        "new-thread-status",
        "new-thread-empty",
        "new-thread-results",
    ):
        assert f'id="{element_id}"' in html


def test_new_conversation_panel_starts_hidden(client):
    """The picker must not be shown by default - only after the user
    explicitly opts to start a new conversation."""
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    html = resp.text
    panel_start = html.index('id="new-thread-panel"')
    tag_slice = html[max(0, panel_start - 40) : panel_start + 40]
    assert 'class="hidden"' in tag_slice


def test_new_conversation_js_calls_users_endpoint(client):
    resp = client.get("/ui/explore-messages.js")
    assert resp.status_code == 200
    js = resp.text
    assert "/users?" in js
    assert "searchNewThreadUsers" in js
    assert "toggleNewThreadPanel" in js


def test_new_conversation_js_reuses_existing_thread_flow_not_a_duplicate(client):
    """The new picker must call the same startThreadWith(userId, username)
    the Explore feed's "Message" button already uses - not a second,
    parallel DM-opening implementation."""
    resp = client.get("/ui/explore-messages.js")
    assert resp.status_code == 200
    js = resp.text
    assert "startThreadWith(btn.dataset.userId, btn.dataset.username)" in js
    # Exactly one function definition for opening a thread - confirms the
    # new picker calls into it rather than reimplementing openThread/fetch
    # logic itself.
    assert js.count("function startThreadWith(") == 1


def test_new_conversation_js_never_bypasses_auth_headers(client):
    resp = client.get("/ui/explore-messages.js")
    assert resp.status_code == 200
    js = resp.text
    fetch_index = js.index("fetch(`/users?")
    # authHeaders() must appear on the same fetch call, not merely
    # somewhere else in the file.
    nearby = js[fetch_index : fetch_index + 200]
    assert "authHeaders()" in nearby


def test_existing_explore_message_button_still_present(client):
    """Regression guard: the pre-existing per-post "Message" button (the
    only prior DM-initiation path) must still exist unchanged - the new
    picker is additive, not a replacement."""
    resp = client.get("/ui/explore-posts.js")
    assert resp.status_code == 200
    js = resp.text
    assert "startThreadWithFromButton" in js
    assert "startThreadWith(btn.dataset.userId, btn.dataset.username)" in js
