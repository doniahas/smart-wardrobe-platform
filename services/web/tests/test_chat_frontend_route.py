"""Proves the served browser frontend actually
has a user-facing AI chat surface, and that it talks to the queue/job
architecture (`POST /chat/jobs` + `GET /chat/jobs/{id}`) rather than
bypassing it with the synchronous `POST /chat` endpoint.

There's no frontend/JS test-execution tooling in this project (see
docs/testing.md's "Known gaps") - these are lightweight, honest content
assertions against the exact static files FastAPI serves under `/ui/`
(`app/static/index.html`, `app/static/chat.js`), the same style already
used by test_root_redirect.py for the static mount itself. They can't run
the JS, but they do prove the shipped artifact contains the chat UI markup
and that its fetch calls target the queued job endpoints, not the
synchronous one - which is what "the frontend's normal chat path points at
the queued endpoint" actually means for a plain HTML/JS app with no build
step.
"""


def test_served_index_html_has_a_chat_tab_and_form(client):
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    html = resp.text

    # A nav entry a logged-in user can actually click to reach chat.
    assert 'id="nav-chat"' in html
    assert "showChatTab()" in html

    # The chat surface itself: a message log, an input, a submit form.
    assert 'id="chat-app"' in html
    assert 'id="chat-form"' in html
    assert 'id="chat-input"' in html
    assert 'id="chat-log"' in html

    assert 'src="./chat.js"' in html


def test_chat_js_is_served(client):
    resp = client.get("/ui/chat.js")
    assert resp.status_code == 200
    assert "chat" in resp.text.lower()


def test_chat_js_submits_through_the_queued_jobs_endpoint(client):
    resp = client.get("/ui/chat.js")
    assert resp.status_code == 200
    js = resp.text

    # Submission goes to the persistent priority-queue job endpoint...
    assert "fetch('/chat/jobs'" in js
    assert "method: 'POST'" in js

    # ...and completion is retrieved by polling the per-job GET endpoint.
    assert "/chat/jobs/${jobId}" in js or "/chat/jobs/" in js


def test_chat_js_never_calls_the_synchronous_chat_endpoint(client):
    """The normal browser flow must not bypass the queue: chat.js must not
    contain a fetch call to the bare synchronous `POST /chat` endpoint
    (as distinct from `/chat/jobs`, which it does use)."""
    resp = client.get("/ui/chat.js")
    assert resp.status_code == 200
    js = resp.text

    assert "fetch('/chat'" not in js
    assert 'fetch("/chat"' not in js
