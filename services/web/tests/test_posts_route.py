import pytest


def _post_payload(**overrides) -> dict:
    payload = {"title": "My title", "body": "My body text"}
    payload.update(overrides)
    return payload


# --- create ---


@pytest.mark.asyncio
async def test_create_post_requires_auth(client):
    resp = client.post("/posts", data=_post_payload())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_post_success(client, make_user):
    user = await make_user("postauthor")
    resp = client.post("/posts", data=_post_payload(), headers=user["headers"])
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "My title"
    assert body["body"] == "My body text"
    assert body["is_anonymous"] is False
    assert body["author_id"] == user["user_id"]
    assert body["author_username"] == "postauthor"
    assert body["likes_count"] == 0
    assert body["dislikes_count"] == 0
    assert body["comments_count"] == 0
    assert body["my_reaction"] is None
    assert body["id"]
    assert body["created_at"]


@pytest.mark.asyncio
async def test_create_post_rejects_blank_title(client, make_user):
    user = await make_user("blanktitleuser")
    resp = client.post("/posts", data=_post_payload(title="   "), headers=user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_post_rejects_blank_body(client, make_user):
    user = await make_user("blankbodyuser")
    resp = client.post("/posts", data=_post_payload(body="   "), headers=user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_post_rejects_title_over_max_length(client, make_user):
    user = await make_user("longtitleuser")
    resp = client.post(
        "/posts", data=_post_payload(title="x" * 151), headers=user["headers"]
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_post_accepts_title_at_max_length(client, make_user):
    user = await make_user("maxtitleuser")
    resp = client.post(
        "/posts", data=_post_payload(title="x" * 150), headers=user["headers"]
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_anonymous_post_hides_author_in_response(client, make_user):
    user = await make_user("anonauthor")
    resp = client.post(
        "/posts", data=_post_payload(is_anonymous=True), headers=user["headers"]
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_anonymous"] is True
    assert body["author_id"] is None
    assert body["author_username"] is None


# --- public feed ---


@pytest.mark.asyncio
async def test_list_posts_requires_auth(client):
    resp = client.get("/posts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_posts_shows_other_users_posts(client, make_user):
    author = await make_user("feedauthor")
    reader = await make_user("feedreader")
    client.post("/posts", data=_post_payload(), headers=author["headers"])

    resp = client.get("/posts", headers=reader["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["author_username"] == "feedauthor"


@pytest.mark.asyncio
async def test_list_posts_newest_first(client, make_user):
    user = await make_user("orderuser")
    client.post("/posts", data=_post_payload(title="first"), headers=user["headers"])
    client.post("/posts", data=_post_payload(title="second"), headers=user["headers"])

    resp = client.get("/posts", headers=user["headers"])
    titles = [item["title"] for item in resp.json()["items"]]
    assert titles == ["second", "first"]


@pytest.mark.asyncio
async def test_list_posts_hides_anonymous_author(client, make_user):
    user = await make_user("anonfeeduser")
    client.post("/posts", data=_post_payload(is_anonymous=True), headers=user["headers"])

    resp = client.get("/posts", headers=user["headers"])
    item = resp.json()["items"][0]
    assert item["is_anonymous"] is True
    assert item["author_id"] is None
    assert item["author_username"] is None


@pytest.mark.asyncio
async def test_list_posts_pagination_has_more(client, make_user):
    user = await make_user("pageuser")
    for i in range(3):
        client.post("/posts", data=_post_payload(title=f"post{i}"), headers=user["headers"])

    resp = client.get("/posts?page=1&page_size=2", headers=user["headers"])
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True

    resp = client.get("/posts?page=2&page_size=2", headers=user["headers"])
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_list_posts_rejects_page_size_over_max(client, make_user):
    user = await make_user("overmaxuser")
    resp = client.get("/posts?page_size=101", headers=user["headers"])
    assert resp.status_code == 422


# --- author_id filter (lightweight public-profile view) ---


@pytest.mark.asyncio
async def test_list_posts_author_id_filters_to_that_author(client, make_user):
    author = await make_user("profileauthor")
    other = await make_user("profileother")
    client.post("/posts", data=_post_payload(title="mine"), headers=author["headers"])
    client.post("/posts", data=_post_payload(title="not mine"), headers=other["headers"])

    resp = client.get(f"/posts?author_id={author['user_id']}", headers=other["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "mine"
    assert body["items"][0]["author_username"] == "profileauthor"


@pytest.mark.asyncio
async def test_list_posts_author_id_excludes_that_authors_anonymous_posts(client, make_user):
    """Filtering by author_id must never undo an author's own choice to post
    anonymously - the profile view groups posts by author, so an anonymous
    post appearing there would reveal authorship even without showing the
    username on the card itself."""
    author = await make_user("profileanonauthor")
    other = await make_user("profileanonreader")
    client.post("/posts", data=_post_payload(title="named"), headers=author["headers"])
    client.post("/posts", data=_post_payload(title="hidden", is_anonymous=True), headers=author["headers"])

    resp = client.get(f"/posts?author_id={author['user_id']}", headers=other["headers"])
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "named"


@pytest.mark.asyncio
async def test_list_posts_author_id_no_match_returns_empty_not_error(client, make_user):
    user = await make_user("profilenomatchuser")
    resp = client.get("/posts?author_id=000000000000000000000000", headers=user["headers"])
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_list_posts_author_id_garbage_value_returns_empty_not_error(client, make_user):
    """author_id is a plain string filter, not a parsed ObjectId - a
    non-ObjectId-shaped value must not 400/500, just match nothing."""
    user = await make_user("profilegarbageuser")
    resp = client.get("/posts?author_id=not-an-object-id", headers=user["headers"])
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_list_posts_without_author_id_still_returns_everyone(client, make_user):
    """Regression guard: adding the optional filter must not change the
    unfiltered feed's default behavior."""
    author = await make_user("unfilteredauthor")
    other = await make_user("unfilteredother")
    client.post("/posts", data=_post_payload(title="a"), headers=author["headers"])
    client.post("/posts", data=_post_payload(title="b"), headers=other["headers"])

    resp = client.get("/posts", headers=other["headers"])
    titles = {item["title"] for item in resp.json()["items"]}
    assert titles == {"a", "b"}


@pytest.mark.asyncio
async def test_list_posts_author_id_requires_auth(client):
    resp = client.get("/posts?author_id=000000000000000000000000")
    assert resp.status_code == 401


# --- my posts ---


@pytest.mark.asyncio
async def test_my_posts_requires_auth(client):
    resp = client.get("/posts/mine")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_my_posts_scoped_to_owner(client, make_user):
    owner = await make_user("mineowner")
    other = await make_user("mineother")
    client.post("/posts", data=_post_payload(title="mine"), headers=owner["headers"])
    client.post("/posts", data=_post_payload(title="not mine"), headers=other["headers"])

    resp = client.get("/posts/mine", headers=owner["headers"])
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "mine"


@pytest.mark.asyncio
async def test_my_posts_hides_author_for_own_anonymous_post(client, make_user):
    user = await make_user("mineanonuser")
    client.post("/posts", data=_post_payload(is_anonymous=True), headers=user["headers"])

    resp = client.get("/posts/mine", headers=user["headers"])
    item = resp.json()["items"][0]
    assert item["is_anonymous"] is True
    assert item["author_id"] is None
    assert item["author_username"] is None


# --- delete ---


@pytest.mark.asyncio
async def test_delete_post_requires_auth(client, make_user):
    user = await make_user("deleteauthuser")
    create_resp = client.post("/posts", data=_post_payload(), headers=user["headers"])
    post_id = create_resp.json()["id"]

    resp = client.delete(f"/posts/{post_id}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_own_post_succeeds(client, make_user):
    user = await make_user("deleteownuser")
    create_resp = client.post("/posts", data=_post_payload(), headers=user["headers"])
    post_id = create_resp.json()["id"]

    resp = client.delete(f"/posts/{post_id}", headers=user["headers"])
    assert resp.status_code == 204

    feed = client.get("/posts", headers=user["headers"]).json()
    assert feed["items"] == []


@pytest.mark.asyncio
async def test_delete_other_users_post_returns_404(client, make_user):
    """Security check: deleting another user's post must not succeed, and
    must not leak whether the post exists via a different status code."""
    owner = await make_user("victimuser")
    attacker = await make_user("attackeruser")
    create_resp = client.post("/posts", data=_post_payload(), headers=owner["headers"])
    post_id = create_resp.json()["id"]

    resp = client.delete(f"/posts/{post_id}", headers=attacker["headers"])
    assert resp.status_code == 404

    feed = client.get("/posts", headers=owner["headers"]).json()
    assert len(feed["items"]) == 1


@pytest.mark.asyncio
async def test_delete_nonexistent_post_returns_404(client, make_user):
    user = await make_user("deletenonexistuser")
    resp = client.delete("/posts/000000000000000000000000", headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_post_rejects_invalid_id(client, make_user):
    user = await make_user("deleteinvaliduser")
    resp = client.delete("/posts/not-an-object-id", headers=user["headers"])
    assert resp.status_code == 400
