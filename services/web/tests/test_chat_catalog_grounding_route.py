"""End-to-end coverage of catalog grounding wired into POST /chat and
POST /chat/jobs (routers/chat.py). Complements test_chat_catalog_grounding.py
(pure unit tests of the grounding module itself) and mirrors test_chat_route.py's
patching idiom, but dispatches the shared httpx.AsyncClient.post seam by URL
so both the llm service (/generate) and the catalog service (/crawl) can be
faked independently within one request.
"""
import httpx
import pytest
from bson import ObjectId

from app.services.chat_context import PLAIN_TEXT_STYLE_INSTRUCTION


def _dispatch(monkeypatch, *, llm_response=None, crawl_response=None, crawl_exc=None):
    """Patch httpx.AsyncClient.post to answer /generate and /crawl calls
    independently, and record every call made (url, json) in order."""
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


def _llm_ok(text="Sure."):
    return httpx.Response(200, json={"response": text, "model": "qwen2.5:3b"})


def _crawl_ok(products):
    return httpx.Response(200, json={"products": products, "source_status": []})


def _ata_shirt(**overrides):
    product = {
        "source": "shopify_ata",
        "source_url": "https://atawear.co.il/products/1",
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


# --- synchronous /chat -------------------------------------------------


@pytest.mark.asyncio
async def test_relevant_product_question_grounds_the_llm_prompt(client, make_user, monkeypatch):
    user = await make_user("alice")
    calls = _dispatch(
        monkeypatch, llm_response=_llm_ok(), crawl_response=_crawl_ok([_ata_shirt()])
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    crawl_calls = [c for c in calls if c[0].endswith("/crawl")]
    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    assert len(crawl_calls) == 1
    assert crawl_calls[0][1]["clothing_type"] == "top"
    assert crawl_calls[0][1]["sources"] == ["shopify_ata"]

    prompt = generate_calls[0][1]["prompt"]
    assert "[CATALOG_CONTEXT]" in prompt
    assert "STATUS: verified" in prompt
    assert "Black Linen Shirt" in prompt


@pytest.mark.asyncio
async def test_ordinary_chat_never_calls_catalog(client, make_user, monkeypatch):
    user = await make_user("bob")
    calls = _dispatch(monkeypatch, llm_response=_llm_ok())

    resp = client.post(
        "/chat", json={"message": "What colors work well with navy?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    assert all(not url.endswith("/crawl") for url, _ in calls)
    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    assert generate_calls[0][1] == {
        "prompt": f"{PLAIN_TEXT_STYLE_INSTRUCTION}\n\nWhat colors work well with navy?"
    }


@pytest.mark.asyncio
async def test_greeting_never_calls_catalog(client, make_user, monkeypatch):
    user = await make_user("carol")
    calls = _dispatch(monkeypatch, llm_response=_llm_ok())

    resp = client.post("/chat", json={"message": "Hello, how are you?"}, headers=user["headers"])

    assert resp.status_code == 200
    assert all(not url.endswith("/crawl") for url, _ in calls)


@pytest.mark.asyncio
async def test_generic_real_shopping_query_triggers_catalog_across_real_stores(
    client, make_user, monkeypatch
):
    user = await make_user("dave")
    calls = _dispatch(
        monkeypatch,
        llm_response=_llm_ok(),
        crawl_response=_crawl_ok([]),
    )

    resp = client.post(
        "/chat",
        json={"message": "Show me real shoes I could buy because I don't have suitable shoes."},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    crawl_calls = [c for c in calls if c[0].endswith("/crawl")]
    assert len(crawl_calls) == 1
    assert crawl_calls[0][1]["clothing_type"] == "shoes"
    assert "mock_demo" not in crawl_calls[0][1]["sources"]
    assert set(crawl_calls[0][1]["sources"]) == {
        "shopify_shtaim", "shopify_pipa_fashion", "shopify_kahiko_swimwear", "shopify_ata",
    }


@pytest.mark.asyncio
async def test_unverified_user_claim_is_reported_as_not_found(client, make_user, monkeypatch):
    user = await make_user("erin")
    calls = _dispatch(
        monkeypatch,
        llm_response=_llm_ok(),
        crawl_response=_crawl_ok(
            [_ata_shirt(source="shopify_pipa_fashion", clothing_type="bottom",
                        product_name="Blue Cotton Skirt", color="blue", material="cotton")]
        ),
    )

    resp = client.post(
        "/chat",
        json={"message": "I saw a red silk skirt at PIPA for 100 NIS. Is it a good choice?"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    prompt = generate_calls[0][1]["prompt"]
    assert "STATUS: no_match" in prompt
    assert "Blue Cotton Skirt" not in prompt


@pytest.mark.asyncio
async def test_catalog_unavailable_does_not_crash_chat_and_says_so_in_prompt(
    client, make_user, monkeypatch
):
    user = await make_user("frank")
    calls = _dispatch(
        monkeypatch, llm_response=_llm_ok(), crawl_exc=httpx.ConnectError("connection refused")
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    prompt = generate_calls[0][1]["prompt"]
    assert "STATUS: unavailable" in prompt


@pytest.mark.asyncio
async def test_memory_and_catalog_context_coexist(client, make_user, monkeypatch):
    """Memory learning/context must not be broken by catalog grounding -
    both blocks can appear in the same prompt."""
    user = await make_user("grace")
    put_resp = client.put("/memory", json={"liked_colors": ["blue"]}, headers=user["headers"])
    assert put_resp.status_code == 200

    calls = _dispatch(
        monkeypatch, llm_response=_llm_ok(), crawl_response=_crawl_ok([_ata_shirt()])
    )

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    prompt = generate_calls[0][1]["prompt"]
    assert "[CATALOG_CONTEXT]" in prompt
    assert "[WARDROBE_PREFERENCE_CONTEXT]" in prompt
    assert "Liked colors: blue" in prompt
    assert "[USER_MESSAGE]\nDoes ATA sell a black linen shirt?\n[/USER_MESSAGE]" in prompt


# --- Named-store fallback: store + purchase intent, no recognized clothing type -----------
# Proves the fix end-to-
# end through both real chat entry points: no catalog call is made (there's
# no clothing_type to crawl for), but the LLM still receives an explicit
# STATUS=needs_clarification block rather than the raw, ungrounded message.


@pytest.mark.asyncio
async def test_store_plus_intent_no_clothing_type_grounds_sync_chat_without_a_crawl_call(
    client, make_user, monkeypatch
):
    user = await make_user("judy")
    calls = _dispatch(monkeypatch, llm_response=_llm_ok())

    resp = client.post(
        "/chat",
        json={"message": "I'm pretty sure PIPA sells this red silk piece — is that true?"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    # No real product/attribute claim was ever crawled for - the catalog
    # service must never be called for this message.
    assert all(not url.endswith("/crawl") for url, _ in calls)

    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    prompt = generate_calls[0][1]["prompt"]
    assert "[CATALOG_CONTEXT]" in prompt
    assert "STATUS: needs_clarification" in prompt
    assert "STATUS: verified" not in prompt
    assert "STATUS: no_match" not in prompt


@pytest.mark.asyncio
async def test_async_job_grounds_the_same_clarification_case_before_queueing(
    client, db, make_user, monkeypatch
):
    """Sync and queued chat must apply the exact same named-store fallback behavior - the
    job is built once at submission time, same as every other catalog-
    grounding case (see test_async_job_is_grounded_before_being_queued
    above)."""
    user = await make_user("karl")
    calls = _dispatch(monkeypatch)

    resp = client.post(
        "/chat/jobs",
        json={"message": "I'm pretty sure PIPA sells this red silk piece — is that true?"},
        headers=user["headers"],
    )

    assert resp.status_code == 202
    assert all(not url.endswith("/crawl") for url, _ in calls)

    job_id = resp.json()["job_id"]
    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert "[CATALOG_CONTEXT]" in stored["prompt"]
    assert "STATUS: needs_clarification" in stored["prompt"]
    assert stored["status"] == "pending"


# --- Structural shopping-intent fallback: the same needs_clarification fallback,
# but for a message with NO named store - only a common, unmapped garment
# word ("leggings") plus clear purchase intent. Proves the fix end-to-end
# through the real synchronous route: previously this exact message never
# reached the catalog-grounding block at all (detect_catalog_intent
# returned None), so the LLM would have answered completely ungrounded.


@pytest.mark.asyncio
async def test_no_store_unmapped_garment_request_grounds_sync_chat_without_a_crawl_call(
    client, make_user, monkeypatch
):
    user = await make_user("liam")
    calls = _dispatch(monkeypatch, llm_response=_llm_ok())

    resp = client.post(
        "/chat",
        json={"message": "Find me black leggings to buy."},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    # No item type could be resolved for "leggings" - genuinely nothing to
    # crawl for - so, exactly like the named-store fallback case, no /crawl
    # call is made at all.
    assert all(not url.endswith("/crawl") for url, _ in calls)

    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    prompt = generate_calls[0][1]["prompt"]
    assert "[CATALOG_CONTEXT]" in prompt
    assert "STATUS: needs_clarification" in prompt
    assert "STATUS: verified" not in prompt
    assert "STATUS: no_match" not in prompt


@pytest.mark.asyncio
async def test_no_store_unmapped_garment_request_returns_the_clarification_message(
    client, make_user, monkeypatch
):
    """End-to-end through the guardrail too: whatever the LLM actually
    says, the client-visible reply is the safe, honest clarification
    message (the status-aware guardrail, applied to this newly-detected
    intent) - never a confident-sounding but unverified product claim."""
    user = await make_user("noah")
    _dispatch(
        monkeypatch,
        llm_response=_llm_ok("Sure, there's a great pair of leggings for 80 ILS!"),
    )

    resp = client.post(
        "/chat",
        json={"message": "Find me black leggings to buy."},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    reply = resp.json()["response"]
    assert "80" not in reply
    assert "I can't verify" in reply


# --- asynchronous /chat/jobs --------------------------------------------


@pytest.mark.asyncio
async def test_async_job_is_grounded_before_being_queued(client, db, make_user, monkeypatch):
    user = await make_user("heidi")
    _dispatch(monkeypatch, crawl_response=_crawl_ok([_ata_shirt()]))

    resp = client.post(
        "/chat/jobs",
        json={"message": "Does ATA sell a black linen shirt?"},
        headers=user["headers"],
    )

    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert "[CATALOG_CONTEXT]" in stored["prompt"]
    assert "STATUS: verified" in stored["prompt"]
    assert stored["status"] == "pending"


@pytest.mark.asyncio
async def test_async_job_skips_catalog_for_irrelevant_message(client, db, make_user, monkeypatch):
    user = await make_user("ivan")
    calls = _dispatch(monkeypatch)

    resp = client.post(
        "/chat/jobs", json={"message": "Hello, how are you?"}, headers=user["headers"]
    )

    assert resp.status_code == 202
    assert all(not url.endswith("/crawl") for url, _ in calls)
    job_id = resp.json()["job_id"]
    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert stored["prompt"] == f"{PLAIN_TEXT_STYLE_INSTRUCTION}\n\nHello, how are you?"


# --- Ambiguous get/find tier: the same needs_clarification fallback, for a
# no-store, unrecognized-garment "get"-phrased shopping question
# ("Do you know where I can get a bodysuit?") - closing the false negative
# independent review identified (detect_catalog_intent previously returned
# None for this exact message, reaching the LLM with zero grounding).
# Mirrors the "to buy" leggings coverage above one section up, proving the
# fix end-to-end through both real chat execution paths, not just at the
# detect_catalog_intent() unit level (see test_chat_catalog_grounding.py).


@pytest.mark.asyncio
async def test_no_store_ambiguous_get_request_grounds_sync_chat_without_a_crawl_call(
    client, make_user, monkeypatch
):
    user = await make_user("olga")
    calls = _dispatch(monkeypatch, llm_response=_llm_ok())

    resp = client.post(
        "/chat",
        json={"message": "Do you know where I can get a bodysuit?"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    # No item type could be resolved for "bodysuit" - genuinely nothing to
    # crawl for - so no /crawl call is made at all.
    assert all(not url.endswith("/crawl") for url, _ in calls)

    generate_calls = [c for c in calls if c[0].endswith("/generate")]
    prompt = generate_calls[0][1]["prompt"]
    assert "[CATALOG_CONTEXT]" in prompt
    assert "STATUS: needs_clarification" in prompt
    assert "STATUS: verified" not in prompt
    assert "STATUS: no_match" not in prompt


@pytest.mark.asyncio
async def test_no_store_ambiguous_get_request_returns_the_clarification_message(
    client, make_user, monkeypatch
):
    """End-to-end guardrail proof: even if the LLM fabricates a specific
    store, product, and link for the unrecognized garment, the client-
    visible reply is the safe, deterministic clarification message -
    never the fabricated text. This is exactly the hallucination this ambiguous get/find tier
    fix exists to prevent from reaching a real user."""
    user = await make_user("piotr")
    _dispatch(
        monkeypatch,
        llm_response=_llm_ok(
            "Yes! TrendyThreads has a great bodysuit for $29.99 - "
            "check it out at trendythreads.example/bodysuit-1"
        ),
    )

    resp = client.post(
        "/chat",
        json={"message": "Do you know where I can get a bodysuit?"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    reply = resp.json()["response"]
    assert "TrendyThreads" not in reply
    assert "29.99" not in reply
    assert "trendythreads.example" not in reply
    assert "I can't verify" in reply


@pytest.mark.asyncio
async def test_async_job_grounds_the_same_ambiguous_get_case_before_queueing(
    client, db, make_user, monkeypatch
):
    """Sync and queued chat must apply the exact same ambiguous get/find behavior - the
    job is built once at submission time, same as every other catalog-
    grounding case (see test_async_job_is_grounded_before_being_queued
    above)."""
    user = await make_user("querida")
    calls = _dispatch(monkeypatch)

    resp = client.post(
        "/chat/jobs",
        json={"message": "Do you know where I can get a bodysuit?"},
        headers=user["headers"],
    )

    assert resp.status_code == 202
    assert all(not url.endswith("/crawl") for url, _ in calls)

    job_id = resp.json()["job_id"]
    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert "[CATALOG_CONTEXT]" in stored["prompt"]
    assert "STATUS: needs_clarification" in stored["prompt"]
    assert stored["status"] == "pending"