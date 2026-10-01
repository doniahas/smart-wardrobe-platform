"""Hallucination-robustness follow-up: hard, code-level verification of the
LLM's *reply* against the exact catalog products retrieved for a chat
request (services/chat_catalog_grounding.py::validate_grounded_reply).

Two layers of coverage:
  - Pure unit tests of validate_grounded_reply/get_catalog_grounding
    directly (no network, no DB) - the six scenarios named in the task.
  - End-to-end tests through the real POST /chat and POST /chat/jobs entry
    points (mirroring test_chat_catalog_grounding_route.py's patching
    idiom), proving the guardrail is actually wired into both the
    synchronous route and the queued-job worker path, not just the helper
    function in isolation.
"""
import httpx
import pytest
from bson import ObjectId

from app.models import CatalogProductPublic
from app.services.chat_catalog_grounding import (
    CATALOG_UNAVAILABLE_MESSAGE,
    NEEDS_CLARIFICATION_MESSAGE,
    NO_VERIFIED_PRODUCT_MESSAGE,
    CatalogGrounding,
    _fallback_reply,
    get_catalog_grounding,
    validate_grounded_reply,
)
from app.services.chat_queue import create_chat_job
from app.services.chat_worker import process_one_job

PRODUCT_A = CatalogProductPublic(
    source="shopify_ata",
    source_url="https://atawear.co.il/products/black-linen-shirt",
    product_name="Black Linen Shirt",
    clothing_type="top",
    color="black",
    material="linen",
    price=199.0,
    currency="ILS",
    availability="in_stock",
)


def _raw_product(**overrides) -> dict:
    product = {
        "source": "shopify_ata",
        "source_url": "https://atawear.co.il/products/black-linen-shirt",
        "source_product_id": "ATA-1",
        "product_name": "Black Linen Shirt",
        "clothing_type": "top",
        "color": "black",
        "material": "linen",
        "price": 199.0,
        "currency": "ILS",
        "image_url": "https://atawear.co.il/images/1.jpg",
        "availability": "in_stock",
        "fetched_at": "2025-01-01T00:00:00Z",
    }
    product.update(overrides)
    return product


def _patch_crawl(monkeypatch, *, llm_response=None, crawl_response=None, crawl_exc=None):
    calls = []

    async def fake_post(self, url, json=None, **kwargs):
        calls.append((url, json))
        if url.endswith("/crawl"):
            if crawl_exc is not None:
                raise crawl_exc
            return crawl_response
        if url.endswith("/generate"):
            return llm_response
        raise AssertionError(f"unexpected outbound call: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def _llm_ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"response": text, "model": "qwen2.5:3b"})


def _crawl_ok(products: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"products": products, "source_status": []})


# --- unit: validate_grounded_reply -----------------------------------------


def test_reply_citing_the_retrieved_product_is_still_replaced_with_trusted_facts():
    """Trusted-fields replacement: even when the LLM's reply correctly cites Product A's
    real URL, its own free text is never trusted for the product's facts -
    the reply is replaced by a code-rendered listing built only from
    Product A's trusted structured fields, since the model could have
    gotten the URL right while still misstating a color/price/etc."""
    reply = f"I'd suggest the Black Linen Shirt: {PRODUCT_A.source_url}"

    result = validate_grounded_reply(reply, (PRODUCT_A,))

    assert result != reply
    assert PRODUCT_A.product_name in result
    assert PRODUCT_A.source_url in result


def test_product_a_source_url_is_preserved_exactly_in_the_rendered_facts():
    """The code-rendered replacement must still carry the real URL
    byte-for-byte - the guardrail only ever discards the LLM's own text,
    never mangles a trusted field it renders itself."""
    reply = f"Here's a great option: {PRODUCT_A.source_url}"

    result = validate_grounded_reply(reply, (PRODUCT_A,))

    assert PRODUCT_A.source_url in result
    assert result == _fallback_reply((PRODUCT_A,))


def test_reply_inventing_an_unretrieved_product_is_not_returned_as_verified():
    """catalog returns Product A, LLM invents Product B -> Product B must
    not be returned as a verified recommendation."""
    invented_reply = (
        "I recommend the Sunset Boho Dress: https://totally-fake-store.example.com/sunset-boho"
    )

    result = validate_grounded_reply(invented_reply, (PRODUCT_A,))

    assert "Sunset Boho Dress" not in result
    assert "totally-fake-store.example.com" not in result
    # The guardrail's fallback still offers the one real product instead of
    # inventing a replacement.
    assert PRODUCT_A.source_url in result
    assert PRODUCT_A.product_name in result


def test_no_catalog_match_reply_cannot_fabricate_a_product():
    """no catalog match (empty, but grounded, product set) -> the model
    cannot fabricate a product; the reply is replaced with an explicit
    "nothing verified" message."""
    fabricated_reply = "Sure! The Emerald Wrap Dress is available at https://example.com/emerald"

    result = validate_grounded_reply(fabricated_reply, ())

    assert result == NO_VERIFIED_PRODUCT_MESSAGE
    assert "Emerald Wrap Dress" not in result


def test_catalog_unavailable_reply_is_replaced_with_the_honest_unavailable_message():
    """Status-aware guardrail: superseded expectation. Before this
    fix, an honest-*looking* reply with no URL passed through unchanged
    purely because it cited nothing to reject - but the same "no URL
    present" shortcut also let an actively fabricated claim through (see
    test_zero_verified_products_url_less_specific_claim_is_rejected). There
    is no reliable way to tell "genuinely honest" apart from "confidently
    wrong" by inspecting arbitrary LLM text, so the model's own wording is
    no longer trusted in this state at all - status="unavailable" always
    produces the same deterministic, honest CATALOG_UNAVAILABLE_MESSAGE,
    regardless of what the model said."""
    honest_sounding_reply = "I can't verify current availability or pricing for that right now."

    result = validate_grounded_reply(honest_sounding_reply, (), status="unavailable")

    assert result == CATALOG_UNAVAILABLE_MESSAGE


def test_catalog_unavailable_reply_replaces_even_a_confident_fabricated_claim():
    """The same status="unavailable" outcome, but with a reply that
    actively fabricates availability - proving the fix isn't merely
    "leave honest-looking text alone," it uniformly replaces every reply
    in this state."""
    fabricated_reply = "Sure, ATA has it in stock right now for 99 ILS."

    result = validate_grounded_reply(fabricated_reply, (), status="unavailable")

    assert result == CATALOG_UNAVAILABLE_MESSAGE
    assert "99" not in result


def test_needs_clarification_reply_is_replaced_with_the_clarification_message():
    """The named-store fallback's needs_clarification outcome must not let the model state or
    confirm a specific product claim either - it gets its own honest,
    clarification-prompting message, not the generic no_match one."""
    reply_confirming_a_claim = "Yes, PIPA does sell that red silk piece."

    result = validate_grounded_reply(reply_confirming_a_claim, (), status="needs_clarification")

    assert result == NEEDS_CLARIFICATION_MESSAGE
    assert "red silk" not in result.lower()


def test_no_match_status_still_uses_the_generic_no_verified_product_message():
    """Regression guard: no_match (and any missing/unrecognized status)
    keeps using the original generic message - only unavailable/
    needs_clarification get their own, more specific wording."""
    result = validate_grounded_reply("Sure! It's a lovely dress.", (), status="no_match")

    assert result == NO_VERIFIED_PRODUCT_MESSAGE


def test_ordinary_conversation_reply_is_never_incorrectly_rejected():
    """Ordinary conversation with no product recommendation (grounding was
    never attempted at all, products=None) must never be touched, even if
    it happens to contain an unrelated URL."""
    reply = "You could check out https://some-style-blog.example.com for outfit ideas!"

    result = validate_grounded_reply(reply, None)

    assert result == reply


def test_fallback_never_invents_a_replacement_product():
    from app.services.chat_catalog_grounding import _fallback_reply

    other_product = PRODUCT_A.model_copy(
        update={
            "source_url": "https://atawear.co.il/products/navy-cotton-dress",
            "product_name": "Navy Cotton Dress",
        }
    )

    fallback = _fallback_reply((PRODUCT_A, other_product))

    assert PRODUCT_A.product_name in fallback
    assert PRODUCT_A.source_url in fallback
    assert other_product.product_name in fallback
    assert other_product.source_url in fallback


def test_fallback_with_no_products_tells_user_nothing_is_verified():
    from app.services.chat_catalog_grounding import _fallback_reply

    assert _fallback_reply(()) == NO_VERIFIED_PRODUCT_MESSAGE


# --- Hallucination grounding: URL-less
# unsupported claim after zero verified products. Regression test.
#
# Before this fix, validate_grounded_reply's zero-verified-products branch
# only rejected a reply that cited a URL not in the (always-empty)
# allowed set. A reply with NO url at all vacuously satisfied
# `all(url in allowed_urls for url in _extract_urls(reply))` (True over an
# empty iterable) and was returned completely unchanged - so a specific,
# unsupported external-product factual claim with no URL slipped through
# whenever the real catalog found nothing to verify. This test intentionally
# does NOT rely on any URL-based assertion, per this task's instruction.


def test_zero_verified_products_url_less_specific_claim_is_rejected():
    """The exact confirmed gap: grounding was attempted, the real catalog
    verified ZERO products, and the LLM's reply makes a specific,
    unverified external-product factual claim with no URL at all. This
    must never reach the client unchanged - there is no verified catalog
    record backing "a green linen shirt for 150 ILS" at ATA."""
    unsupported_claim = "ATA has a green linen shirt for 150 ILS."

    result = validate_grounded_reply(unsupported_claim, ())

    assert result != unsupported_claim
    assert "green linen" not in result.lower()
    assert "150" not in result


def test_user_false_claim_echoed_back_by_the_llm_is_not_treated_as_verified():
    """A core hallucination concern, applied here: a client's own (false)
    factual claim about a product must not become a "verified" fact merely
    because the LLM's reply agrees with/repeats it. The catalog found
    nothing, so an LLM reply that echoes the user's claim back as
    confirmed must be rejected exactly like any other unsupported claim in
    this state - no URL, no invented-URL, just an agreeable-sounding
    confirmation."""
    reply_confirming_the_users_own_claim = (
        "Yes, that's correct - the green linen shirt for 150 ILS is real."
    )

    result = validate_grounded_reply(reply_confirming_the_users_own_claim, ())

    assert result == NO_VERIFIED_PRODUCT_MESSAGE
    assert "150" not in result
    assert "green linen" not in result.lower()


def test_multiple_verified_products_render_only_their_own_trusted_facts():
    """Case K at the public validate_grounded_reply() level (not just
    _fallback_reply directly): with more than one verified product, the
    LLM's own text is discarded and every rendered fact still traces back
    to one of the actually-retrieved CatalogProductPublic records - never
    a third, invented product."""
    other_product = PRODUCT_A.model_copy(
        update={
            "source_url": "https://atawear.co.il/products/navy-cotton-dress",
            "product_name": "Navy Cotton Dress",
            "color": "navy",
            "material": "cotton",
        }
    )
    reply = "I only recommend the amazing Emerald Wrap Dress, trust me."

    result = validate_grounded_reply(reply, (PRODUCT_A, other_product))

    assert "Emerald Wrap Dress" not in result
    assert PRODUCT_A.product_name in result
    assert PRODUCT_A.source_url in result
    assert other_product.product_name in result
    assert other_product.source_url in result


# --- Issue 1 (catalog-grounding-season): trusted-only product facts -------
# The LLM must never originate a real product's factual attributes, even
# when it also happens to cite the correct URL - see validate_grounded_
# reply's docstring for the full contract this section locks in.


def test_real_product_facts_are_rendered_from_trusted_structured_data():
    """Every fact in the rendered reply must come from CatalogProductPublic
    itself, regardless of what (if anything) the LLM's reply said."""
    reply = "some arbitrary LLM text, ignored for a verified product"

    result = validate_grounded_reply(reply, (PRODUCT_A,))

    assert PRODUCT_A.product_name in result
    assert PRODUCT_A.color in result
    assert PRODUCT_A.material in result
    assert str(PRODUCT_A.price) in result
    assert PRODUCT_A.currency in result
    assert PRODUCT_A.availability in result
    assert PRODUCT_A.source_url in result


def test_wrong_llm_generated_color_cannot_replace_the_trusted_value():
    """The LLM claims a color that contradicts the real, trusted product
    (black) - the wrong color must never appear in the final reply, and the
    real color must."""
    wrong_reply = f"Great news - we have it in white! {PRODUCT_A.source_url}"

    result = validate_grounded_reply(wrong_reply, (PRODUCT_A,))

    assert "white" not in result.lower()
    assert "black" in result.lower()


def test_wrong_llm_generated_material_cannot_replace_the_trusted_value():
    """The LLM claims a material that contradicts the real, trusted product
    (linen) - the wrong material must never appear in the final reply, and
    the real material must."""
    wrong_reply = f"It's actually 100% cashmere! {PRODUCT_A.source_url}"

    result = validate_grounded_reply(wrong_reply, (PRODUCT_A,))

    assert "cashmere" not in result.lower()
    assert "linen" in result.lower()


def test_wrong_llm_generated_price_cannot_replace_the_trusted_value():
    """The LLM claims a price that contradicts the real, trusted product
    (199.0 ILS) - the wrong price must never appear, and the real price
    must."""
    wrong_reply = f"It's only 49 ILS! Grab it here: {PRODUCT_A.source_url}"

    result = validate_grounded_reply(wrong_reply, (PRODUCT_A,))

    assert "49" not in result
    assert "199" in result


def test_missing_product_field_does_not_produce_an_invented_value():
    """A product with no color/material/price/season trusted at all must
    never have those facts appear in the rendered reply - not even a
    placeholder - regardless of anything the LLM's reply claimed about
    them."""
    bare_product = CatalogProductPublic(
        source="shopify_shtaim",
        source_url="https://shtaim.store/products/mystery-item",
        product_name="Mystery Item",
        clothing_type="accessory",
        availability="unknown",
    )
    reply_claiming_facts = "It's a beautiful red silk item for $25!"

    result = validate_grounded_reply(reply_claiming_facts, (bare_product,))

    assert "red" not in result.lower()
    assert "silk" not in result.lower()
    assert "25" not in result
    assert bare_product.product_name in result
    assert bare_product.source_url in result
    # No color/material/season line at all - not even an empty/placeholder one.
    assert "color:" not in result
    assert "material:" not in result
    assert "season:" not in result


def test_real_verified_url_still_appears_byte_for_byte():
    result = validate_grounded_reply("irrelevant text", (PRODUCT_A,))

    assert PRODUCT_A.source_url in result


def test_trusted_season_is_included_only_when_verified():
    from app.models import CatalogSeason

    winter_product = PRODUCT_A.model_copy(update={"season": CatalogSeason.WINTER})

    result = validate_grounded_reply("irrelevant text", (winter_product,))

    assert "season: winter" in result


def test_unknown_season_never_appears_as_if_it_were_a_fact():
    # PRODUCT_A has no season set - defaults to CatalogSeason.UNKNOWN.
    result = validate_grounded_reply("irrelevant text", (PRODUCT_A,))

    assert "season:" not in result
    assert "unknown" not in result.lower()


# --- unit: get_catalog_grounding structured products -----------------------


@pytest.mark.asyncio
async def test_get_catalog_grounding_returns_structured_products_for_verified(monkeypatch):
    _patch_crawl(monkeypatch, crawl_response=_crawl_ok([_raw_product()]))

    grounding = await get_catalog_grounding("Does ATA sell a black linen shirt?")

    assert isinstance(grounding, CatalogGrounding)
    assert grounding.context_text is not None
    assert grounding.products is not None
    assert len(grounding.products) == 1
    assert grounding.products[0].source_url == PRODUCT_A.source_url
    assert grounding.products[0].product_name == "Black Linen Shirt"
    assert grounding.status == "verified"


@pytest.mark.asyncio
async def test_get_catalog_grounding_products_is_empty_tuple_for_no_match(monkeypatch):
    _patch_crawl(monkeypatch, crawl_response=_crawl_ok([]))

    grounding = await get_catalog_grounding("Does ATA sell a black linen shirt?")

    assert grounding.context_text is not None
    assert grounding.products == ()
    assert grounding.status == "no_match"


@pytest.mark.asyncio
async def test_get_catalog_grounding_status_is_unavailable_on_catalog_failure(monkeypatch):
    _patch_crawl(monkeypatch, crawl_exc=httpx.ConnectError("connection refused"))

    grounding = await get_catalog_grounding("Does ATA sell a black linen shirt?")

    assert grounding.products == ()
    assert grounding.status == "unavailable"


@pytest.mark.asyncio
async def test_get_catalog_grounding_status_is_needs_clarification_for_gap_2_case(monkeypatch):
    called = False

    async def fake_post(self, url, json=None, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    grounding = await get_catalog_grounding(
        "I'm pretty sure PIPA sells this red silk piece — is that true?"
    )

    assert called is False
    assert grounding.products == ()
    assert grounding.status == "needs_clarification"


@pytest.mark.asyncio
async def test_get_catalog_grounding_products_is_none_for_ordinary_conversation(monkeypatch):
    called = False

    async def fake_post(self, url, json=None, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    grounding = await get_catalog_grounding("What colors work well with navy?")

    assert grounding.context_text is None
    assert grounding.products is None
    assert grounding.status is None
    assert called is False


# --- end-to-end: synchronous POST /chat -------------------------------------


@pytest.mark.asyncio
async def test_chat_route_replaces_correctly_cited_reply_with_trusted_facts(
    client, make_user, monkeypatch
):
    """Trusted-fields replacement, end-to-end: even a reply that correctly cites Product
    A's real URL is replaced with the code-rendered trusted-facts listing -
    the LLM's own phrasing is never trusted for a real product's facts."""
    user = await make_user("guardrail-alice")
    _patch_crawl(
        monkeypatch,
        llm_response=_llm_ok(f"I'd suggest the Black Linen Shirt: {PRODUCT_A.source_url}"),
        crawl_response=_crawl_ok([_raw_product()]),
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    reply = resp.json()["response"]
    assert reply != f"I'd suggest the Black Linen Shirt: {PRODUCT_A.source_url}"
    assert PRODUCT_A.product_name in reply
    assert PRODUCT_A.source_url in reply


@pytest.mark.asyncio
async def test_chat_route_replaces_invented_product_with_grounded_fallback(
    client, make_user, monkeypatch
):
    user = await make_user("guardrail-bob")
    _patch_crawl(
        monkeypatch,
        llm_response=_llm_ok(
            "I recommend the Sunset Boho Dress: https://totally-fake-store.example.com/sunset"
        ),
        crawl_response=_crawl_ok([_raw_product()]),
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    reply = resp.json()["response"]
    assert "Sunset Boho Dress" not in reply
    assert "totally-fake-store.example.com" not in reply
    assert PRODUCT_A.source_url in reply


@pytest.mark.asyncio
async def test_chat_route_no_match_reply_cannot_fabricate_a_product(
    client, make_user, monkeypatch
):
    user = await make_user("guardrail-carol")
    _patch_crawl(
        monkeypatch,
        llm_response=_llm_ok("Sure, the Emerald Wrap Dress is at https://example.com/emerald"),
        crawl_response=_crawl_ok([]),
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    assert resp.json()["response"] == NO_VERIFIED_PRODUCT_MESSAGE


@pytest.mark.asyncio
async def test_chat_route_catalog_unavailable_returns_the_honest_unavailable_message(
    client, make_user, monkeypatch
):
    """Status-aware guardrail: superseded expectation, end-to-end -
    see test_catalog_unavailable_reply_is_replaced_with_the_honest_
    unavailable_message's docstring for why the LLM's own wording (even
    when it sounds honest) is no longer trusted once the catalog is
    unavailable."""
    user = await make_user("guardrail_dave")
    _patch_crawl(
        monkeypatch,
        llm_response=_llm_ok("I can't verify current availability for that right now."),
        crawl_exc=httpx.ConnectError("connection refused"),
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    assert resp.json()["response"] == CATALOG_UNAVAILABLE_MESSAGE


@pytest.mark.asyncio
async def test_chat_route_zero_verified_products_url_less_claim_is_rejected(
    client, make_user, monkeypatch
):
    """Status-aware guardrail, end-to-end through the real
    synchronous /chat route: the exact confirmed gap
    (test_zero_verified_products_url_less_specific_claim_is_rejected),
    proven through the full route rather than only the guardrail function
    in isolation."""
    user = await make_user("guardrail_heidi")
    _patch_crawl(
        monkeypatch,
        llm_response=_llm_ok("ATA has a green linen shirt for 150 ILS."),
        crawl_response=_crawl_ok([]),
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    reply = resp.json()["response"]
    assert reply == NO_VERIFIED_PRODUCT_MESSAGE
    assert "150" not in reply


@pytest.mark.asyncio
async def test_chat_route_ordinary_conversation_with_url_is_not_incorrectly_rejected(
    client, make_user, monkeypatch
):
    user = await make_user("guardrail_erin")
    _patch_crawl(
        monkeypatch,
        llm_response=_llm_ok("Check out https://some-style-blog.example.com for ideas!"),
    )

    resp = client.post(
        "/chat", json={"message": "What colors work well with navy?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    assert resp.json()["response"] == "Check out https://some-style-blog.example.com for ideas!"


# --- end-to-end: async POST /chat/jobs + chat_worker ------------------------


@pytest.mark.asyncio
async def test_chat_jobs_worker_replaces_correctly_cited_reply_with_trusted_facts(
    client, db, make_user, monkeypatch
):
    """Trusted-fields replacement via the async job/worker path: same as the synchronous
    route - a correctly-cited reply is still replaced with trusted,
    code-rendered facts rather than trusted verbatim."""
    user = await make_user("guardrail_frank")
    _patch_crawl(monkeypatch, crawl_response=_crawl_ok([_raw_product()]))

    resp = client.post(
        "/chat/jobs",
        json={"message": "Does ATA sell a black linen shirt?"},
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert stored["catalog_products"] is not None
    assert stored["catalog_products"][0]["source_url"] == PRODUCT_A.source_url

    async def fake_call_llm(prompt):
        return f"I'd suggest the Black Linen Shirt: {PRODUCT_A.source_url}"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    assert await process_one_job(db) is True

    completed = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert completed["status"] == "completed"
    assert completed["result"] != f"I'd suggest the Black Linen Shirt: {PRODUCT_A.source_url}"
    assert PRODUCT_A.product_name in completed["result"]
    assert PRODUCT_A.source_url in completed["result"]


@pytest.mark.asyncio
async def test_chat_jobs_worker_rejects_invented_product_in_reply(
    client, db, make_user, monkeypatch
):
    user = await make_user("guardrail_grace")
    _patch_crawl(monkeypatch, crawl_response=_crawl_ok([_raw_product()]))

    resp = client.post(
        "/chat/jobs",
        json={"message": "Does ATA sell a black linen shirt?"},
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    async def fake_call_llm(prompt):
        return "I recommend the Sunset Boho Dress: https://totally-fake-store.example.com/sunset"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    assert await process_one_job(db) is True

    completed = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert completed["status"] == "completed"
    assert "Sunset Boho Dress" not in completed["result"]
    assert PRODUCT_A.source_url in completed["result"]


@pytest.mark.asyncio
async def test_chat_jobs_worker_zero_verified_products_url_less_claim_is_rejected(
    client, db, make_user, monkeypatch
):
    """Status-aware guardrail, async path: the same confirmed gap as
    test_chat_route_zero_verified_products_url_less_claim_is_rejected,
    proven through the queued-job/worker path instead of the synchronous
    route - both entry points must obey the identical safety rule (case M).
    Also proves the new `catalog_status` field is correctly persisted on
    the job document and read back by the worker, not just the in-memory
    CatalogGrounding object the synchronous route uses directly."""
    user = await make_user("guardrail_ivan")
    _patch_crawl(monkeypatch, crawl_response=_crawl_ok([]))

    resp = client.post(
        "/chat/jobs",
        json={"message": "Does ATA sell a black linen shirt?"},
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert stored["catalog_products"] == []
    assert stored["catalog_status"] == "no_match"

    async def fake_call_llm(prompt):
        return "ATA has a green linen shirt for 150 ILS."

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    assert await process_one_job(db) is True

    completed = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert completed["status"] == "completed"
    assert completed["result"] == NO_VERIFIED_PRODUCT_MESSAGE
    assert "150" not in completed["result"]


@pytest.mark.asyncio
async def test_chat_jobs_worker_catalog_unavailable_returns_honest_message(
    client, db, make_user, monkeypatch
):
    """Async-path parity for the unavailable outcome too - not just
    no_match - reinforcing case M across every zero-verified-products
    status, not only the one exercised synchronously above."""
    user = await make_user("guardrail_judy")
    _patch_crawl(monkeypatch, crawl_exc=httpx.ConnectError("connection refused"))

    resp = client.post(
        "/chat/jobs",
        json={"message": "Does ATA sell a black linen shirt?"},
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert stored["catalog_status"] == "unavailable"

    async def fake_call_llm(prompt):
        return "Sure, ATA has it in stock for 99 ILS."

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    assert await process_one_job(db) is True

    completed = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert completed["result"] == CATALOG_UNAVAILABLE_MESSAGE
    assert "99" not in completed["result"]


@pytest.mark.asyncio
async def test_chat_jobs_worker_leaves_ordinary_conversation_untouched(db, monkeypatch):
    """A job created without catalog grounding (catalog_products=None, the
    default - e.g. every pre-existing test/caller that doesn't pass it)
    must never be touched by the guardrail."""
    doc = await create_chat_job(db, "user-1", "What should I wear?")

    async def fake_call_llm(prompt):
        return "Check out https://some-style-blog.example.com for ideas!"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    assert await process_one_job(db) is True

    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["result"] == "Check out https://some-style-blog.example.com for ideas!"
