"""Style/Occasion multi-select pass: style/occasion are now lists on a
wardrobe item (a garment may fit more than one of each - see models.py).
Covers the API/model plumbing (create, update, list/get) for multiple
values and backward compatibility with items that predate this change.

Regression coverage in particular for PUT /wardrobe/{id} with more than
one style/occasion value: FastAPI 0.111's Form-list binding only calls
FormData.getlist() (collecting every repeated `style=` value) when it
recognizes the parameter's annotation as a sequence shape - an
`Optional[List[X]] = Form(None)` annotation defeats that recognition and
silently falls back to a single (last-wins) value even when the client
sent several, which is exactly what routers/wardrobe.py::update_item's
`style`/`occasion` params must avoid (see that function's own comment).

Uses real multipart with repeated keys (the `files=` list-of-tuples
trick - httpx's TestClient doesn't multipart-encode a plain `data=`
list-of-tuples the way a browser's FormData does) so these tests exercise
the same wire shape the frontend's multi-select actually sends.
"""
import pytest


def _multipart_fields(**fields) -> list:
    """Turns {"style": ["casual", "minimal"], "name": "Item"} into the
    files= list-of-tuples shape that produces one repeated multipart field
    per list entry, and one field for any plain scalar value."""
    entries = []
    for key, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            entries.append((key, (None, v)))
    return entries


def _create_item(client, user, style="casual", occasion="casual", **overrides):
    fields = {
        "name": "Item",
        "clothing_type": "top",
        "color": "olive",
        "season": "all",
        "style": style,
        "occasion": occasion,
    }
    fields.update(overrides)
    return client.post("/wardrobe", files=_multipart_fields(**fields), headers=user["headers"])


@pytest.mark.asyncio
async def test_create_item_with_multiple_styles_and_occasions(client, make_user):
    user = await make_user("styleocc_create_multi")
    resp = _create_item(client, user, style=["casual", "minimal"], occasion=["university", "travel"])

    assert resp.status_code == 201
    body = resp.json()
    assert body["style"] == ["casual", "minimal"]
    assert body["occasion"] == ["university", "travel"]


@pytest.mark.asyncio
async def test_create_item_requires_at_least_one_style(client, make_user):
    user = await make_user("styleocc_create_empty")
    fields = _multipart_fields(
        name="Item", clothing_type="top", color="olive", season="all", occasion="casual",
    )  # no style at all
    resp = client.post("/wardrobe", files=fields, headers=user["headers"])
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_update_item_with_multiple_styles_and_occasions_persists_all_values(
    client, make_user, make_wardrobe_item
):
    """Regression test for the FastAPI Form-list binding bug: PUT with
    several style/occasion values must persist every one of them, not
    silently collapse to the last value sent."""
    user = await make_user("styleocc_update_multi")
    item_id = await make_wardrobe_item(user["user_id"], style="casual", occasion="casual")

    resp = client.put(
        f"/wardrobe/{item_id}",
        files=_multipart_fields(style=["casual", "minimal"], occasion=["university", "travel"]),
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["style"] == ["casual", "minimal"]
    assert body["occasion"] == ["university", "travel"]


@pytest.mark.asyncio
async def test_update_item_with_a_single_style_still_works(client, make_user, make_wardrobe_item):
    """The single-value case must keep working alongside the multi-value
    fix above - a one-element list is still a valid list."""
    user = await make_user("styleocc_update_single")
    item_id = await make_wardrobe_item(user["user_id"], style="casual", occasion="casual")

    resp = client.put(
        f"/wardrobe/{item_id}",
        files=_multipart_fields(style="formal", occasion="work"),
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["style"] == ["formal"]
    assert body["occasion"] == ["work"]


@pytest.mark.asyncio
async def test_update_item_omitting_style_leaves_it_untouched(client, make_user, make_wardrobe_item):
    """A partial update that only sends occasion must not clear or alter
    style - style=None (the Form default when the key is absent entirely)
    means "not provided", not "clear this field"."""
    user = await make_user("styleocc_update_partial")
    item_id = await make_wardrobe_item(user["user_id"], style="casual", occasion="casual")

    resp = client.put(
        f"/wardrobe/{item_id}",
        files=_multipart_fields(occasion="work"),  # style omitted entirely
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["style"] == ["casual"]
    assert body["occasion"] == ["work"]


@pytest.mark.asyncio
async def test_legacy_scalar_style_and_occasion_load_as_one_element_lists(
    client, make_user, make_wardrobe_item
):
    """A wardrobe document from before this change (make_wardrobe_item's
    fixture doc stores plain scalar strings) must still load through GET
    (list and single) as one-element lists, not fail validation."""
    user = await make_user("styleocc_legacy")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Legacy shirt", style="casual", occasion="work"
    )

    list_resp = client.get("/wardrobe", headers=user["headers"])
    assert list_resp.status_code == 200
    listed = next(item for item in list_resp.json() if item["id"] == item_id)
    assert listed["style"] == ["casual"]
    assert listed["occasion"] == ["work"]

    get_resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    assert get_resp.status_code == 200
    assert get_resp.json()["style"] == ["casual"]


@pytest.mark.asyncio
async def test_filter_by_style_matches_item_whose_style_list_contains_it(
    client, make_user, make_wardrobe_item
):
    """GET /wardrobe?style= stays a single-value filter (membership, not
    equality) - it must match an item whose style list merely *contains*
    the requested value, not only an item whose style is exactly that one
    value."""
    user = await make_user("styleocc_filter_membership")
    await make_wardrobe_item(
        user["user_id"], name="Multi-style top", style=["casual", "minimal"], occasion="casual"
    )
    await make_wardrobe_item(
        user["user_id"], name="Formal top", style=["formal"], occasion="work"
    )

    resp = client.get("/wardrobe", params={"style": "minimal"}, headers=user["headers"])

    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert names == ["Multi-style top"]


# --- PUT /wardrobe/{id} remove_image=true clears the photo ---
#
# The route originally only exposed `image_url: Optional[str] = Form(None)`
# as the way to set image_url directly, and it was initially assumed a
# client could clear a photo by sending image_url="". That assumption was
# proven wrong empirically while adding this test: FastAPI/Starlette parses
# an *empty* multipart field as the parameter being absent (-> None) for an
# Optional[str] = Form(None) parameter, not as an explicit "" - confirmed
# live with `curl -F "image_url="` (identical wire shape to a real browser
# FormData.append('image_url', '')) and, to rule out an image_url-specific
# quirk, `curl -F "color_hex="` against the same route - both produced the
# same "No fields to update" 400 this route already had. `remove_image:
# bool = Form(False)` (a dedicated flag, not an ambiguous empty string) is
# the actual fix - see update_item's own comment for the same reasoning.
#
# Not really a style/occasion concern, but reuses this file's existing
# PUT /wardrobe/{id} multipart helpers rather than duplicating them in a
# new file for two tests.


@pytest.mark.asyncio
async def test_put_wardrobe_item_with_remove_image_true_clears_the_photo(
    client, make_user, make_wardrobe_item
):
    user = await make_user("removephoto_user")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Has A Photo", image_url="/uploads/wardrobe/existing123.jpg",
    )

    resp = client.put(
        f"/wardrobe/{item_id}",
        files=_multipart_fields(remove_image="true"),
        headers=user["headers"],
    )

    assert resp.status_code == 200
    # Falsy either way (None or "") - what actually matters is that it's no
    # longer the real stored URL, matching how the frontend itself treats
    # image_url (item.image_url ? <img> : placeholder emoji - see
    # app.js::itemCard).
    assert not resp.json()["image_url"]

    get_resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    assert get_resp.status_code == 200
    assert not get_resp.json()["image_url"]


@pytest.mark.asyncio
async def test_put_wardrobe_item_with_empty_image_url_and_no_remove_image_flag_is_a_noop(
    client, make_user, make_wardrobe_item
):
    """Regression guard for the exact bug this fix replaces: confirms
    image_url="" alone (the old, broken mechanism) is genuinely a no-op at
    the HTTP layer - i.e. that this is why remove_image was needed at all,
    not just a hypothetical concern."""
    user = await make_user("emptyimageurl_user")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Has A Photo", image_url="/uploads/wardrobe/existing789.jpg",
    )

    resp = client.put(
        f"/wardrobe/{item_id}",
        files=_multipart_fields(image_url=""),
        headers=user["headers"],
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_wardrobe_item_replacement_image_takes_precedence_over_remove_image(
    client, make_user, make_wardrobe_item
):
    """If both a new file and remove_image=true are sent in one request,
    the replacement wins - matches update_item's own precedence comment."""
    user = await make_user("replaceoverremove_user")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Has A Photo", image_url="/uploads/wardrobe/existing000.jpg",
    )

    fields = _multipart_fields(remove_image="true")
    fields.append(("image", ("new.jpg", b"\xff\xd8\xff\xe0fakejpegbytes", "image/jpeg")))

    resp = client.put(f"/wardrobe/{item_id}", files=fields, headers=user["headers"])

    # A real image upload goes through save_wardrobe_image's real content
    # validation (Pillow decode-and-verify) - this fake byte string isn't a
    # genuine JPEG, so it's expected to be rejected. What this test actually
    # proves is precedence: remove_image=true alone (previous test) always
    # succeeds with a 200, so a 400 here (validation failure) rather than a
    # 200 with the photo cleared confirms the image branch was taken
    # instead of the remove_image branch, not that the whole request failed
    # for some other reason.
    assert resp.status_code == 400
    assert "image" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_put_wardrobe_item_without_image_fields_leaves_existing_photo_untouched(
    client, make_user, make_wardrobe_item
):
    """Regression guard: an ordinary edit (no image change requested at
    all) must never accidentally clear an existing photo."""
    user = await make_user("keepphoto_user")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Has A Photo", image_url="/uploads/wardrobe/existing456.jpg",
    )

    resp = client.put(
        f"/wardrobe/{item_id}",
        files=_multipart_fields(name="Renamed"),
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["image_url"] is not None
    assert "existing456.jpg" in resp.json()["image_url"]


# --- [object Object] frontend fix follow-up: pin the exact FastAPI shape ---


@pytest.mark.asyncio
async def test_over_length_name_produces_fastapi_validation_array(client, make_user):
    """Pins the exact shape that caused the [object Object] bug: a Form
    field exceeding FastAPI's own max_length constraint returns a 422
    whose detail is a list of validation-error objects with a string
    `msg` field - not a plain string. The frontend's formatApiError()
    (app.js) is what now turns this into a safe display string."""
    user = await make_user("longnameuser")
    resp = _create_item(client, user, name="x" * 101)  # max_length=100

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert len(detail) >= 1
    assert all(isinstance(e.get("msg"), str) for e in detail)
