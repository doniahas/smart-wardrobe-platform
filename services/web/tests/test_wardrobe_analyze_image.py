import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app.services import image_storage


def _image_bytes(fmt: str = "JPEG", size=(20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


def _patch_llm_response(monkeypatch, response: httpx.Response = None, exc=None):
    """Patch the outbound call to the llm service (analyze-image goes
    through the same httpx.AsyncClient.post as call_llm/call_llm_vision)."""

    async def fake_post(self, url, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _model_response(payload: dict, model: str = "moondream:latest") -> httpx.Response:
    """Build the llm service's own /generate response shape: {"response":
    "<json string produced by the vision model>", "model": "..."}."""
    return httpx.Response(200, json={"response": json.dumps(payload), "model": model})


def _patch_llm_responses_sequence(monkeypatch, responses: list):
    """Like _patch_llm_response, but returns a different item for each
    successive outbound call in order - needed for the clothing_type
    verification check (Design C), which makes a second, independent call
    after the production one. An item that's an Exception instance is
    raised instead of returned, matching _patch_llm_response's `exc`
    behavior. Returns a dict a test can inspect afterward to assert
    exactly how many calls were actually made (e.g. proving the
    verification call is skipped entirely when there's nothing to
    verify)."""
    calls = list(responses)
    call_count = {"n": 0}

    async def fake_post(self, url, **kwargs):
        call_count["n"] += 1
        item = calls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return call_count


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    """Redirect saved uploads to a throwaway directory, exactly like
    test_image_storage.py - analyze-image must never write here."""
    monkeypatch.setattr(image_storage, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_analyze_image_happy_path(client, make_user, upload_dir, monkeypatch):
    user = await make_user("analyzeuser_happy")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Navy long-sleeve tee",
                "clothing_type": "top",
                "color": "navy",
                "season": "winter",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "name": "Navy long-sleeve tee",
        "clothing_type": "top",
        "color": "navy",
        "color_hex": "#1f2d50",
        "season": "winter",
        "style": ["casual"],
        "occasion": ["casual"],
        "model": "moondream:latest",
    }


@pytest.mark.asyncio
async def test_analyze_image_requires_auth(upload_dir, client):
    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_analyze_image_invalid_image_returns_400(client, make_user, upload_dir):
    user = await make_user("analyzeuser_invalid")

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("not_a_photo.jpg", b"just plain text, not an image", "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_analyze_image_oversized_returns_413(client, make_user, upload_dir, monkeypatch):
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_BYTES", 10)
    user = await make_user("analyzeuser_oversized")
    data = _image_bytes()
    assert len(data) > 10

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", data, "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_analyze_image_llm_unavailable_returns_503(client, make_user, upload_dir, monkeypatch):
    user = await make_user("analyzeuser_unavailable")
    _patch_llm_response(monkeypatch, exc=httpx.ConnectError("connection refused"))

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_analyze_image_llm_timeout_returns_504(client, make_user, upload_dir, monkeypatch):
    user = await make_user("analyzeuser_timeout")
    _patch_llm_response(
        monkeypatch,
        response=httpx.Response(504, json={"detail": "Ollama did not respond in time."}),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_analyze_image_malformed_model_output_returns_null_suggestions(
    client, make_user, upload_dir, monkeypatch
):
    """The LLM call itself succeeded, but the model's text wasn't valid
    JSON - this is not an error, just nothing useful to suggest."""
    user = await make_user("analyzeuser_malformed")
    _patch_llm_response(
        monkeypatch,
        response=httpx.Response(
            200, json={"response": "not valid json at all", "model": "moondream:latest"}
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] is None
    assert body["clothing_type"] is None
    assert body["color"] is None
    assert body["color_hex"] is None
    assert body["season"] is None
    assert body["style"] is None
    assert body["occasion"] is None
    assert body["model"] == "moondream:latest"


@pytest.mark.asyncio
async def test_analyze_image_invalid_enum_values_become_null(client, make_user, upload_dir, monkeypatch):
    user = await make_user("analyzeuser_badenum")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Some item",
                "clothing_type": "hat",  # not a valid ClothingType
                "color": "blue",
                "season": "rainy",  # not a valid Season
                "style": ["goth"],  # not a valid Style
                "occasion": ["party"],  # not a valid Occasion
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Some item"
    assert body["color"] == "blue"
    assert body["color_hex"] == "#2b6cb0"
    assert body["clothing_type"] is None
    assert body["season"] is None
    assert body["style"] is None
    assert body["occasion"] is None


@pytest.mark.asyncio
async def test_analyze_image_empty_strings_become_null(client, make_user, upload_dir, monkeypatch):
    user = await make_user("analyzeuser_blank")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "",
                "clothing_type": "top",
                "color": "   ",
                "season": None,
                "style": ["casual"],
                "occasion": None,
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] is None
    assert body["color"] is None
    assert body["color_hex"] is None
    assert body["clothing_type"] == "top"
    assert body["style"] == ["casual"]
    assert body["occasion"] is None


@pytest.mark.asyncio
async def test_analyze_image_non_list_style_becomes_null(client, make_user, upload_dir, monkeypatch):
    """A non-compliant model response with a bare scalar for style (instead
    of the array the schema asks for) is not coerced into a one-element
    list - it's dropped, same "null rather than guess" convention as
    everything else the model might get wrong."""
    user = await make_user("analyzeuser_style_scalar")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": "red",
                "season": "all",
                "style": "casual",
                "occasion": "casual",
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["style"] is None
    assert body["occasion"] is None


@pytest.mark.asyncio
async def test_analyze_image_multiple_style_and_occasion_values_are_kept(
    client, make_user, upload_dir, monkeypatch
):
    """A garment that genuinely fits more than one style/occasion keeps
    every one the model confidently suggested, not just the first."""
    user = await make_user("analyzeuser_multi_tags")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Minimal travel tee",
                "clothing_type": "top",
                "color": "gray",
                "season": "all",
                "style": ["casual", "minimal"],
                "occasion": ["university", "travel"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["style"] == ["casual", "minimal"]
    assert body["occasion"] == ["university", "travel"]


@pytest.mark.asyncio
async def test_analyze_image_style_suggestions_are_deduplicated_and_capped(
    client, make_user, upload_dir, monkeypatch
):
    """Repeats collapse to one entry, invalid entries are dropped in
    place (not treated as filler), and the result never exceeds the
    schema's own maxItems bound even if the model's raw JSON does."""
    user = await make_user("analyzeuser_dedupe_cap")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": "black",
                "season": "all",
                "style": ["casual", "casual", "not-a-style", "minimal", "elegant", "formal"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["style"] == ["casual", "minimal", "elegant"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_name",
    ["Casual", "casual", "  Casual  ", "Formal", "Spring", "Streetwear", "University"],
)
async def test_analyze_image_name_exactly_matching_a_tag_value_becomes_null(
    client, make_user, upload_dir, monkeypatch, raw_name
):
    """Regression test for the trousers photo that came back with
    name="Casual", clothing_type="shoes": the model reused one of its own
    Style/Occasion/Season values as the whole `name` instead of actually
    describing the garment. Only an exact (case-insensitive,
    whitespace-trimmed) match is nulled - see the next test for the
    "merely contains" boundary."""
    user = await make_user(f"analyzeuser_nametag_{len(raw_name)}_{raw_name.strip().lower()}")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": raw_name,
                "clothing_type": "bottom",
                "color": "khaki",
                "season": "spring",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] is None
    # Nulling `name` must not affect any other field.
    assert body["clothing_type"] == "bottom"
    assert body["style"] == ["casual"]
    assert body["occasion"] == ["casual"]


@pytest.mark.asyncio
async def test_analyze_image_name_containing_a_tag_word_is_left_untouched(
    client, make_user, upload_dir, monkeypatch
):
    """A real description that merely contains a style/occasion/season
    word (rather than being exactly equal to one) is a legitimate name,
    not a field-bleed artifact - only whole-string equality triggers the
    guard."""
    user = await make_user("analyzeuser_nametag_substring")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Casual Friday blazer",
                "clothing_type": "outerwear",
                "color": "navy",
                "season": "all",
                "style": ["formal"],
                "occasion": ["work"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Casual Friday blazer"


@pytest.mark.asyncio
async def test_analyze_image_name_matching_a_clothing_type_value_is_left_untouched(
    client, make_user, upload_dir, monkeypatch
):
    """ClothingType values are deliberately excluded from the tag-value
    guard: unlike a style/occasion/season word, "Top" or "Dress" alone is
    still a plausible if terse genuine garment name, not obviously a
    field-bleed artifact."""
    user = await make_user("analyzeuser_nametag_clothingtype")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Top",
                "clothing_type": "top",
                "color": "white",
                "season": "summer",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Top"


@pytest.mark.asyncio
async def test_analyze_image_trousers_reproduction_name_cleared_type_not_patched(
    client, make_user, upload_dir, monkeypatch
):
    """Direct reproduction of the reported failure: a tan/khaki trousers
    photo came back with name="Casual" (a Style value used as the name)
    and clothing_type="shoes" (a plain vision misclassification - "bottom"
    is a valid, available ClothingType member, so this isn't a missing
    taxonomy entry, just the model getting it wrong).

    This pass only fixes what's generically fixable without guessing: the
    tag-value name is nulled. clothing_type="shoes" is deliberately NOT
    patched or second-guessed here - there is no generic, non-hardcoded
    signal in this payload that it's wrong (nothing else in the JSON
    contradicts it), so "fixing" it would require exactly the kind of
    garment-keyword dictionary this pass was told not to add. It's a
    model-quality limitation, tracked for re-evaluation once a stronger
    GPU-backed vision model is available - not something this test
    asserts should change."""
    user = await make_user("analyzeuser_trousers_repro")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Casual",
                "clothing_type": "shoes",
                "color": "olive",
                "season": "spring",
                "style": ["casual", "streetwear"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] is None
    assert body["clothing_type"] == "shoes"  # not patched - see docstring
    assert body["style"] == ["casual", "streetwear"]
    assert body["occasion"] == ["casual"]


@pytest.mark.asyncio
async def test_analyze_image_hex_color_inconsistent_with_name_is_reconciled(
    client, make_user, upload_dir, monkeypatch
):
    """Regression test for the olive-green T-shirt bug: the model named a
    chromatic color in `name` ("Green") but `color` came back as a
    grayscale/near-black hex ("#222222") - a "we didn't really look"
    default, not the color it just described. The chromatic word from
    `name` should be used instead of silently keeping the contradictory
    hex."""
    user = await make_user("analyzeuser_hexcolor")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Green",
                "clothing_type": "top",
                "color": "#222222",
                "season": "summer",
                "style": ["casual"],
                "occasion": ["formal"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Green"
    assert body["color"] == "green"
    assert body["color_hex"] == "#2f7d32"


@pytest.mark.asyncio
async def test_analyze_image_hex_color_with_no_reconciling_name_uses_hex_own_name(
    client, make_user, upload_dir, monkeypatch
):
    """Same suspicious hex as above, but nothing in `name` to recover a
    chromatic color from - falls back to the hex's own nearest color name
    ("black" for a near-black hex like "#222222") rather than surfacing
    the raw hex code to the user."""
    user = await make_user("analyzeuser_hexcolor_noname")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "T-shirt",
                "clothing_type": "top",
                "color": "#222222",
                "season": "summer",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["color"] == "black"
    assert body["color_hex"] == "#111111"


@pytest.mark.asyncio
async def test_analyze_image_legitimate_hex_color_is_converted_to_a_name(
    client, make_user, upload_dir, monkeypatch
):
    """A hex value with real saturation (not a grayscale/near-black
    default) is still never shown to the user as a hex code - it's
    converted to its nearest plain color name, here "olive" for olive
    drab (#6B8E23)."""
    user = await make_user("analyzeuser_hexcolor_valid")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Olive tee",
                "clothing_type": "top",
                "color": "#6B8E23",
                "season": "summer",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["color"] == "olive"
    assert body["color_hex"] == "#6b8e23"


@pytest.mark.asyncio
async def test_analyze_image_genuinely_gray_hex_becomes_gray_name(client, make_user, upload_dir, monkeypatch):
    """If the model actually describes a gray garment ("Gray hoodie") and
    the hex is genuinely mid-gray (not dark enough to read as black), the
    result is the plain word "gray", not the raw hex code."""
    user = await make_user("analyzeuser_hexcolor_reallygray")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Gray hoodie",
                "clothing_type": "top",
                "color": "#808080",
                "season": "winter",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["color"] == "gray"
    assert body["color_hex"] == "#888888"


@pytest.mark.asyncio
async def test_analyze_image_ignores_a_model_supplied_color_hex(client, make_user, upload_dir, monkeypatch):
    """The model is never asked for an exact shade (see wardrobe_ai.py's
    _COLOR_INSTRUCTION and _garment_fields' docstring) - color_hex always
    comes from this pipeline's own category-default lookup, never from
    whatever the model's raw JSON happens to contain, even if a
    "color_hex" key is present in it."""
    user = await make_user("analyzeuser_hex_ignored")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": "navy",
                "color_hex": "#ff00ff",
                "season": "all",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["color_hex"] == "#1f2d50"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_color, expected",
    [
        ("olive green", "olive"),
        ("navy blue", "navy"),
        ("burgundy", "red"),
        ("dark gray", "gray"),
        ("Mustard", "yellow"),
        ("teal", "blue"),
    ],
)
async def test_analyze_image_color_phrase_maps_to_palette(
    client, make_user, upload_dir, monkeypatch, raw_color, expected
):
    """A color phrase the model already wrote as words - not a hex code -
    still needs to land on one of the 14 wardrobe palette values, the same
    as every other clothing item's color, regardless of what garment the
    photo happens to show."""
    user = await make_user(f"analyzeuser_palette_{expected}_{len(raw_color)}")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": raw_color,
                "season": "all",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["color"] == expected


@pytest.mark.asyncio
async def test_analyze_image_unrecognized_color_becomes_null(client, make_user, upload_dir, monkeypatch):
    """A color word that matches nothing in the palette is left unset -
    "null rather than guess", same convention as every other suggested
    field - rather than being forced into some arbitrary bucket."""
    user = await make_user("analyzeuser_palette_unmatched")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": "iridescent holographic",
                "season": "all",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["color"] is None
    assert body["color_hex"] is None


@pytest.mark.asyncio
async def test_analyze_image_never_writes_to_uploads(client, make_user, upload_dir, monkeypatch):
    user = await make_user("analyzeuser_nowrite")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": "red",
                "season": "all",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert list(upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_analyze_image_never_writes_to_db(client, make_user, upload_dir, db, monkeypatch):
    user = await make_user("analyzeuser_nodbwrite")
    _patch_llm_response(
        monkeypatch,
        response=_model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": "red",
                "season": "all",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        ),
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert await db.wardrobe.count_documents({}) == 0


def _jpeg_with_orientation(orientation, size=(40, 20), color=(200, 50, 30)):
    """A JPEG carrying a real EXIF Orientation tag - not the plain
    orientation-free _image_bytes() helper above, since this specifically
    exercises EXIF handling (see test_image_orientation.py for the
    orientation-normalization unit tests themselves)."""
    img = Image.new("RGB", size, color)
    exif = img.getexif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_analyze_image_sends_orientation_normalized_bytes_to_model(
    client, make_user, upload_dir, monkeypatch
):
    """The actual bug this closes: a sideways EXIF-tagged phone photo must
    reach the vision model already pixel-correct, not as raw sideways
    pixels the model has no way to know it should rotate."""
    user = await make_user("analyzeuser_exif_to_model")
    captured = {}

    async def fake_post(self, url, **kwargs):
        captured["json"] = kwargs.get("json")
        return _model_response(
            {
                "name": "Item",
                "clothing_type": "top",
                "color": "red",
                "season": "all",
                "style": ["casual"],
                "occasion": ["casual"],
            }
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    raw = _jpeg_with_orientation(orientation=6, size=(40, 20))
    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", raw, "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    sent_image_b64 = captured["json"]["images"][0]
    sent_bytes = base64.b64decode(sent_image_b64)
    assert sent_bytes != raw
    with Image.open(io.BytesIO(sent_bytes)) as img:
        assert img.size == (20, 40)  # width/height swapped - normalized, not the raw 40x20
        assert img.getexif().get(0x0112, 1) == 1


# --- clothing_type consistency check (Design C) -----------------------


def _garment_payload(clothing_type) -> dict:
    return {
        "name": "Item",
        "clothing_type": clothing_type,
        "color": "red",
        "season": "all",
        "style": ["casual"],
        "occasion": ["casual"],
    }


@pytest.mark.asyncio
async def test_clothing_type_verification_agreement_keeps_production_value(
    client, make_user, upload_dir, monkeypatch
):
    user = await make_user("analyzeuser_type_verify_agree")
    _patch_llm_responses_sequence(
        monkeypatch,
        [
            _model_response(_garment_payload("bottom")),
            _model_response({"clothing_type": "bottom"}),
        ],
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["clothing_type"] == "bottom"


@pytest.mark.asyncio
async def test_clothing_type_verification_disagreement_nulls_it(
    client, make_user, upload_dir, monkeypatch
):
    """The actual bug this closes: production says one type, the
    verification call says a different one - neither is trusted, and the
    verification call's own answer never overwrites the production
    value."""
    user = await make_user("analyzeuser_type_verify_disagree")
    _patch_llm_responses_sequence(
        monkeypatch,
        [
            _model_response(_garment_payload("bottom")),
            _model_response({"clothing_type": "shoes"}),
        ],
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["clothing_type"] is None
    # Every other field is completely unaffected by the type check.
    assert body["name"] == "Item"
    assert body["color"] == "red"
    assert body["style"] == ["casual"]
    assert body["occasion"] == ["casual"]


@pytest.mark.asyncio
async def test_clothing_type_verification_returns_no_valid_type_nulls_production(
    client, make_user, upload_dir, monkeypatch
):
    """The verification call succeeds but has nothing to confirm with
    (the model itself says null there) - treated the same as an outright
    disagreement, not as "no opinion, trust production"."""
    user = await make_user("analyzeuser_type_verify_null")
    _patch_llm_responses_sequence(
        monkeypatch,
        [
            _model_response(_garment_payload("bottom")),
            _model_response({"clothing_type": None}),
        ],
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["clothing_type"] is None


@pytest.mark.asyncio
async def test_clothing_type_verification_call_failure_degrades_gracefully(
    client, make_user, upload_dir, monkeypatch
):
    """The verification call itself failing (connection error here;
    covers the same code path as a timeout) must never crash or fail the
    overall Analyze Item request - it only means clothing_type can't be
    confirmed, so it's nulled, exactly like an outright disagreement."""
    user = await make_user("analyzeuser_type_verify_failure")
    _patch_llm_responses_sequence(
        monkeypatch,
        [
            _model_response(_garment_payload("bottom")),
            httpx.ConnectError("connection refused"),
        ],
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["clothing_type"] is None
    assert body["name"] == "Item"


@pytest.mark.asyncio
async def test_clothing_type_verification_is_skipped_when_production_type_is_already_null(
    client, make_user, upload_dir, monkeypatch
):
    """No point spending a second call verifying a field the production
    call was already uncertain about - only one outbound call should be
    made at all."""
    user = await make_user("analyzeuser_type_verify_skip")
    call_count = _patch_llm_responses_sequence(
        monkeypatch,
        [_model_response(_garment_payload(None))],
    )

    resp = client.post(
        "/wardrobe/analyze-image",
        files={"image": ("photo.jpg", _image_bytes(), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["clothing_type"] is None
    assert call_count["n"] == 1
