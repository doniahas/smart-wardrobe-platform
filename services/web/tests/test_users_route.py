"""GET /users - authenticated user-discovery for DM initiation.

A client must be able to find and message any
other registered client, not only someone who has already authored a
visible, non-anonymous post. These tests prove the new route stays within
its deliberately narrow scope - id/username only, caller excluded, no
relation to anonymous-post identity - while covering the key discovery
cases.
"""

import pytest


# --- Auth ---


@pytest.mark.asyncio
async def test_list_users_requires_auth(client):
    resp = client.get("/users")
    assert resp.status_code == 401


# --- Basic listing ---


@pytest.mark.asyncio
async def test_caller_is_excluded_from_results(client, make_user):
    caller = await make_user("discoverself")
    resp = client.get("/users", headers=caller["headers"])
    assert resp.status_code == 200
    ids = [u["id"] for u in resp.json()["items"]]
    assert caller["user_id"] not in ids


@pytest.mark.asyncio
async def test_another_registered_user_is_returned(client, make_user):
    caller = await make_user("discovercaller")
    other = await make_user("discoverother")

    resp = client.get("/users", headers=caller["headers"])
    assert resp.status_code == 200
    items = resp.json()["items"]
    matching = [u for u in items if u["id"] == other["user_id"]]
    assert len(matching) == 1
    assert matching[0]["username"] == "discoverother"


@pytest.mark.asyncio
async def test_user_who_has_never_posted_is_still_discoverable(client, make_user):
    """The key case: no post, no comment, no existing
    conversation - discovery must not depend on any forum activity."""
    caller = await make_user("neverpostedcaller")
    lurker = await make_user("neverpostedlurker")
    # Deliberately create no post/comment/message for `lurker` at all.

    resp = client.get("/users", headers=caller["headers"])
    assert resp.status_code == 200
    ids = [u["id"] for u in resp.json()["items"]]
    assert lurker["user_id"] in ids


# --- Field safety ---


@pytest.mark.asyncio
async def test_only_safe_public_fields_are_returned(client, make_user):
    caller = await make_user("fieldscaller")
    await make_user("fieldsother")

    resp = client.get("/users", headers=caller["headers"])
    assert resp.status_code == 200
    for row in resp.json()["items"]:
        assert set(row.keys()) == {"id", "username"}


@pytest.mark.asyncio
async def test_sensitive_fields_never_leak(client, make_user):
    caller = await make_user("sensitivecaller")
    await make_user("sensitiveother")

    resp = client.get("/users", headers=caller["headers"])
    assert resp.status_code == 200
    raw = resp.text.lower()
    # None of these should ever appear anywhere in the response body -
    # not just "not in the modeled fields", but genuinely absent from the
    # wire payload (guards against an accidental **doc passthrough).
    for forbidden in ("hashed_password", "password", "email", "@example.com"):
        assert forbidden not in raw


# --- Search ---


@pytest.mark.asyncio
async def test_search_filters_by_username_substring(client, make_user):
    caller = await make_user("searchcaller")
    await make_user("zzz_target_zzz")
    await make_user("unrelated_name")

    resp = client.get("/users", params={"search": "target"}, headers=caller["headers"])
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()["items"]]
    assert "zzz_target_zzz" in usernames
    assert "unrelated_name" not in usernames


@pytest.mark.asyncio
async def test_search_is_case_insensitive(client, make_user):
    caller = await make_user("caseinsensitivecaller")
    await make_user("CaseTarget")

    resp = client.get("/users", params={"search": "casetarget"}, headers=caller["headers"])
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()["items"]]
    assert "CaseTarget" in usernames


@pytest.mark.asyncio
async def test_search_with_no_match_returns_empty_list_not_an_error(client, make_user):
    caller = await make_user("nomatchcaller")

    resp = client.get(
        "/users", params={"search": "no_such_user_exists_anywhere"}, headers=caller["headers"]
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_search_term_is_not_interpreted_as_regex(client, make_user):
    """A regex-metacharacter-laden search term must be treated as a plain
    literal substring (via re.escape), never as an actual regex pattern -
    otherwise a client could probe query behavior beyond a simple
    substring match."""
    caller = await make_user("regexcaller")
    await make_user("regexsafeuser")

    resp = client.get("/users", params={"search": ".*"}, headers=caller["headers"])
    assert resp.status_code == 200
    # ".*" as a literal substring matches nothing real; as an actual regex
    # it would match every username. Confirms the escape is in effect.
    usernames = [u["username"] for u in resp.json()["items"]]
    assert "regexsafeuser" not in usernames


# --- Pagination bounds ---


@pytest.mark.asyncio
async def test_results_are_paginated(client, make_user):
    caller = await make_user("paginationcaller")
    for i in range(5):
        await make_user(f"paginationuser{i}")

    resp = client.get("/users", params={"page": 1, "page_size": 2}, headers=caller["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True


# --- Anonymity independence ---


@pytest.mark.asyncio
async def test_directory_does_not_reveal_anonymous_post_authorship(client, make_user, make_post):
    """The directory must never let a caller connect a real username back
    to an anonymous post - it only ever lists usernames, never anything
    post-related."""
    caller = await make_user("anonguardcaller")
    author = await make_user("anonguardauthor")
    await make_post(author["user_id"], author_username="anonguardauthor", is_anonymous=True)

    # The post itself still hides authorship correctly (unchanged
    # behavior - regression guard for the pre-existing anonymity feature).
    posts_resp = client.get("/posts", headers=caller["headers"])
    assert posts_resp.status_code == 200
    post = next(p for p in posts_resp.json()["items"] if p["title"] == "Title")
    assert post["is_anonymous"] is True
    assert post["author_id"] is None
    assert post["author_username"] is None

    # The directory lists the real account independently of that post -
    # it was never hiding accounts, only post authorship, and the two are
    # unrelated: the directory response carries no post/anonymity data at
    # all for anyone to connect back to the hidden post.
    users_resp = client.get("/users", headers=caller["headers"])
    assert users_resp.status_code == 200
    usernames = [u["username"] for u in users_resp.json()["items"]]
    assert "anonguardauthor" in usernames
    for row in users_resp.json()["items"]:
        assert "is_anonymous" not in row
        assert "post" not in row


# --- Existing DM behavior is unaffected ---


@pytest.mark.asyncio
async def test_dm_send_still_works_after_discovering_recipient(client, make_user):
    """End-to-end: discover a user via /users, then actually message them
    through the existing, unmodified send route."""
    sender = await make_user("e2ediscoversender")
    recipient = await make_user("e2ediscoverrecipient")

    discover_resp = client.get(
        "/users", params={"search": "e2ediscoverrecipient"}, headers=sender["headers"]
    )
    assert discover_resp.status_code == 200
    found = discover_resp.json()["items"][0]
    assert found["username"] == "e2ediscoverrecipient"

    send_resp = client.post(
        f"/messages/{found['id']}", data={"body": "hi, found you"}, headers=sender["headers"]
    )
    assert send_resp.status_code == 201
    assert send_resp.json()["body"] == "hi, found you"


@pytest.mark.asyncio
async def test_dm_privacy_unaffected_by_new_discovery_route(client, make_user):
    """Regression guard: a third party still cannot read a conversation
    they aren't part of, even though recipient discovery is now easier."""
    sender = await make_user("privacyregsender")
    recipient = await make_user("privacyregrecipient")
    outsider = await make_user("privacyregoutsider")

    send_resp = client.post(
        f"/messages/{recipient['user_id']}", data={"body": "private"}, headers=sender["headers"]
    )
    assert send_resp.status_code == 201

    history_resp = client.get(
        f"/messages/{recipient['user_id']}", headers=outsider["headers"]
    )
    assert history_resp.status_code == 200
    # An outsider reading "their own history with recipient" (which never
    # existed) correctly gets an empty page, not the sender/recipient's
    # private conversation.
    assert history_resp.json()["items"] == []
