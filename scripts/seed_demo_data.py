#!/usr/bin/env python3
"""Cold-seed data - populates a few deterministic demo/synthetic
users, forum posts, comments, and reactions into a fresh (or
already-seeded) Smart Wardrobe environment, so a fresh install doesn't open
as an empty forum.

Deliberately narrow scope - this is a small, fixed demo dataset, not a
general-purpose seeding framework:
    - 3-5 deterministic fake users
    - 5-8 deterministic text-only forum posts (mixing anonymous and
      non-anonymous)
    - a few deterministic comments, always written by a user other than the
      target post's real author
    - a modest, deterministic set of likes/dislikes

Deliberately NOT in scope: wardrobe/outfit seeding, DMs/conversations,
attachments, memory/chat data, and any destructive reset. (Automatic
seed-on-startup is handled outside this script, by the local/dev `seed`
compose service, which simply runs it.) This script only ever creates or reconciles
state - it never deletes anything.

Profiles (see docs/deployment.md's "Production cold-seed" section)
-------------------------------------------------------------------
This script supports two independent, fixed datasets, selected with
`--profile`:

    - `demo` (default): the original demo dataset - `demo_alex` .. `demo_
      elin`, fixed password `DemoSeed123!`, run automatically by the local/
      dev `seed` compose service (see docker-compose.yml) and by
      `scripts/start_demo.sh`. Unchanged by the `production` profile's
      addition - same usernames, same posts/comments/reactions, same
      password, same behavior.
    - `production`: a separate, realistic-looking synthetic dataset with
      plausible customer-style usernames (no `demo_`/`test_`/`qa_`/`e2e_`
      naming), intended for a real deployed environment so it doesn't look
      empty to a visitor without exposing the publicly-documented demo
      credentials there. Its password is never hardcoded - it must be
      supplied via the `PROD_SEED_PASSWORD` environment variable (the
      script fails loudly and does nothing if it's missing when this
      profile is requested). This profile is never wired into any
      automatic path (not `docker-compose.yml`, not
      `docker-compose.prod.yml`, not CI) - it is only ever run manually,
      by a human, against a real deployment. See docs/deployment.md.

This is an HTTP-driven tool, exactly like scripts/chat_load_test.py -
it talks to the REAL running Smart Wardrobe API (registration, login, post/
comment/reaction routes) and never writes to MongoDB directly. That gets
bcrypt password hashing, JWT auth, unique-index handling, and the real
reaction toggle/CAS logic "for free" and always in sync with the real
implementation.

Usage (Docker-only flow - no host Python/pip install needed; the
clean-machine-friendly way to run this, since the `web` image already
has httpx installed via services/web/requirements.txt)
-----------------------------------------------------------------------
    # 1. Start the real stack (if not already running):
    docker compose up -d

    # 2. Copy the script into the running `web` container and run it there,
    #    using the container's own already-installed dependencies:
    docker compose cp scripts/seed_demo_data.py web:/tmp/seed_demo_data.py
    docker compose exec web python /tmp/seed_demo_data.py

Safe to run once, twice, many times, or after being interrupted halfway
through - see "Idempotency" below. `--base-url` defaults to
http://localhost:8000 because the documented flow above runs *inside* the
`web` container itself (a separate one-off container was deliberately
avoided for this manual flow).

Idempotency / partial-failure safety
-------------------------------------
No single global "already seeded" shortcut is used anywhere in this script
- a previous run could have been interrupted after creating some objects
but before others, so every seeded object is checked and reconciled
*individually*, every run, in a fixed order (users -> posts -> comments ->
reactions):
    - Users: POST /auth/register, treating both 201 (newly created) and 409
      (already exists) as success, then always POST /auth/login to obtain a
      fresh token either way. Registration returns no token (verified from
      services/web/app/routers/auth.py - UserPublic has no access_token
      field), so login is not optional.
    - Posts: for each seed post, list the authoring user's own posts
      (GET /posts/mine, paginated in full) and look for an exact title
      match before creating.
    - Comments: for each seed comment, list the target post's comments
      (GET /posts/{id}/comments, paginated in full) and look for an exact
      (author_username, body) match before creating.
    - Reactions: for each seed reaction, read the reacting user's *current*
      reaction on the target first (from the same paginated list responses,
      which already include `my_reaction` for the viewer) and only POST a
      change when the current state differs from the desired state - never
      blindly re-POST, since POST .../reactions is a toggle and re-sending
      an already-correct value would remove it.

Auth rate limiting
-------------------
RATE_LIMIT_AUTH defaults to 5/minute, keyed by source IP, with /auth/
register and /auth/login on SEPARATE buckets (services/web/app/core/
limiter.py uses key_style="endpoint", confirmed against
services/web/tests/test_rate_limiting.py). With <=5 seed users this script
never needs more than 5 register calls and 5 login calls, comfortably at
(not over) the default limit - but every auth call still goes through a
bounded retry/backoff on HTTP 429 rather than assuming it will never
happen (a shared IP with other recent activity, or a lower configured
limit, can still trigger one). This app's Limiter is not configured with
rate-limit headers (no `headers_enabled=True` in core/limiter.py), so a
`Retry-After` header is used opportunistically if present, but the retry
schedule does not depend on it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass

try:
    import httpx
except ImportError:
    print("This script requires httpx: pip install httpx", file=sys.stderr)
    raise SystemExit(1)


class SeedError(Exception):
    """Raised on any unrecoverable failure - the script always fails loudly
    and stops rather than continuing in an unknown/partially-applied state.
    A later re-run remains safe (see module docstring) even after this."""


# Bounded retry/backoff shared by every write call. Not busy-looping (each
# retry sleeps for real time first) and not unbounded (fails clearly after
# the last attempt). The delays are conservative relative to a 1-minute
# rate-limit window without depending on any particular reset alignment.
_RETRY_DELAYS_SECONDS = [3.0, 6.0, 12.0, 24.0, 48.0]


async def _request_with_retry(
    client: httpx.AsyncClient, method: str, url: str, **kwargs
) -> httpx.Response:
    """Issue one HTTP request, retrying only on 429 with bounded backoff.

    Any other response (including other error statuses) is returned as-is
    for the caller to interpret - this helper's only job is not to let a
    routine, expected 429 abort the whole run.
    """
    last_response: httpx.Response | None = None
    for attempt, delay in enumerate([0.0] + _RETRY_DELAYS_SECONDS, start=1):
        if delay:
            print(
                f"    -> 429 rate-limited on {method} {url} "
                f"(attempt {attempt}/{len(_RETRY_DELAYS_SECONDS) + 1}); "
                f"waiting {delay:.0f}s before retrying..."
            )
            await asyncio.sleep(delay)

        response = await client.request(method, url, **kwargs)
        if response.status_code != 429:
            return response
        last_response = response

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                await asyncio.sleep(max(0.0, float(retry_after)))
            except ValueError:
                pass  # non-numeric Retry-After - fall back to the fixed schedule above

    assert last_response is not None
    return last_response


# --- Seed dataset (fixed, deterministic - see module docstring) ---

_DEMO_PASSWORD = "DemoSeed123!"
# Not "@example.com"/"@...invalid" - EmailStr (email-validator) rejects
# RFC 2606 reserved domains outright. Mirrors the exact working domain
# pattern already proven in scripts/chat_load_test.py.
_DEMO_EMAIL_DOMAIN = "demoseed-smartwardrobe.com"

# Production profile: email is never rendered anywhere in the UI
# (posts/comments only ever expose `author_username` - confirmed against
# services/web/app/models.py) so it only needs to stay syntactically valid
# and collision-free, not "plausible" the way the username has to be. Same
# working domain *pattern* as the demo profile above, just a different,
# clearly-seed-owned subdomain.
_PROD_EMAIL_DOMAIN = "prodseed-smartwardrobe.com"
# Never hardcoded - see _resolve_password() below and the module docstring's
# "Profiles" section. The env var name itself is not a secret.
_PROD_SEED_PASSWORD_ENV_VAR = "PROD_SEED_PASSWORD"


@dataclass
class SeedUser:
    key: str
    username: str
    email_domain: str

    @property
    def email(self) -> str:
        return f"{self.username}@{self.email_domain}"


@dataclass
class SeedPost:
    key: str
    author_key: str
    title: str
    body: str
    is_anonymous: bool


@dataclass
class SeedComment:
    key: str
    post_key: str
    author_key: str
    body: str


@dataclass
class SeedReaction:
    key: str
    target_kind: str  # "post" | "comment"
    target_key: str
    reactor_key: str
    value: int  # 1 (like) or -1 (dislike)


DEMO_SEED_USERS = [
    SeedUser("alex", "demo_alex", _DEMO_EMAIL_DOMAIN),
    SeedUser("bianca", "demo_bianca", _DEMO_EMAIL_DOMAIN),
    SeedUser("chen", "demo_chen", _DEMO_EMAIL_DOMAIN),
    SeedUser("dara", "demo_dara", _DEMO_EMAIL_DOMAIN),
    SeedUser("elin", "demo_elin", _DEMO_EMAIL_DOMAIN),
]

DEMO_SEED_POSTS = [
    SeedPost(
        "closet_by_season",
        "alex",
        "Finally organized my closet by season!",
        "Took me a whole weekend but it was worth it - now I can actually "
        "find things instead of digging through everything at once. "
        "Anyone else group by season instead of by type?",
        is_anonymous=False,
    ),
    SeedPost(
        "matching_shoes",
        "bianca",
        "Anyone else struggle with matching shoes to outfits?",
        "I can plan a whole outfit and then completely blank on what shoes "
        "go with it. Would love tips on how you all think about this.",
        is_anonymous=True,
    ),
    SeedPost(
        "photo_before_laundry",
        "chen",
        "Tip: photograph your outfits before laundry day",
        "Small habit that's saved me more than once - if you liked how an "
        "outfit came together, snap a photo before everything goes back "
        "into the wash and gets separated.",
        is_anonymous=False,
    ),
    SeedPost(
        "capsule_wardrobe_uni",
        "dara",
        "My go-to capsule wardrobe for university",
        "Been rotating the same dozen pieces all semester and it still "
        "feels like I have plenty of outfit options. Happy to share the "
        "exact list if anyone's interested.",
        is_anonymous=True,
    ),
    SeedPost(
        "feature_idea_color",
        "elin",
        "Feature idea: color-coordinated recommendations",
        "Would be great if recommendations leaned harder into color theory "
        "- like flagging when two pieces clash instead of just matching by "
        "season/occasion. Curious what others think.",
        is_anonymous=False,
    ),
    SeedPost(
        "love_outfit_builder",
        "alex",
        "Loving the new outfit builder!",
        "Being able to put together a full outfit from my own wardrobe and "
        "save it has genuinely changed how much I use this app day to day.",
        is_anonymous=False,
    ),
]

DEMO_SEED_COMMENTS = [
    SeedComment(
        "closet_reply_bianca",
        "closet_by_season",
        "bianca",
        "This is so satisfying, I need to do this too!",
    ),
    SeedComment(
        "closet_reply_chen",
        "closet_by_season",
        "chen",
        "Same here, definitely a game changer once you actually do it.",
    ),
    SeedComment(
        "photo_reply_elin",
        "photo_before_laundry",
        "elin",
        "Great tip, doing this from now on.",
    ),
    SeedComment(
        "capsule_reply_alex",
        "capsule_wardrobe_uni",
        "alex",
        "Love this, could you share the full list?",
    ),
    SeedComment(
        "feature_reply_dara",
        "feature_idea_color",
        "dara",
        "Yes please, this would be amazing to have.",
    ),
]

DEMO_SEED_REACTIONS = [
    SeedReaction("react_closet_chen", "post", "closet_by_season", "chen", 1),
    SeedReaction("react_closet_dara", "post", "closet_by_season", "dara", 1),
    SeedReaction("react_matching_elin", "post", "matching_shoes", "elin", -1),
    SeedReaction("react_photo_alex", "post", "photo_before_laundry", "alex", 1),
    SeedReaction("react_capsule_bianca", "post", "capsule_wardrobe_uni", "bianca", 1),
    SeedReaction("react_feature_chen", "post", "feature_idea_color", "chen", 1),
    SeedReaction("react_comment_bianca", "comment", "closet_reply_bianca", "alex", 1),
    SeedReaction("react_comment_alex", "comment", "capsule_reply_alex", "elin", -1),
]


# --- Production profile - a separate, fixed, realistic-looking
# synthetic dataset for a real deployed environment. Deliberately: no
# `demo_`/`test_`/`qa_`/`e2e_` naming anywhere (usernames or content), and
# no hardcoded password (see _resolve_password() below). Otherwise this
# follows the exact same shape/rules as the demo dataset above (comment
# author != post author, text-only, "a few" scale). ---

PROD_SEED_USERS = [
    SeedUser("jordan", "jordan.reyes89", _PROD_EMAIL_DOMAIN),
    SeedUser("mia", "mia.harper22", _PROD_EMAIL_DOMAIN),
    SeedUser("leo", "leo.okafor14", _PROD_EMAIL_DOMAIN),
    SeedUser("sam", "sam.patel07", _PROD_EMAIL_DOMAIN),
    SeedUser("nina", "nina.brooks33", _PROD_EMAIL_DOMAIN),
]

PROD_SEED_POSTS = [
    SeedPost(
        "weekend_closet_reset",
        "jordan",
        "Finally did a full closet reset this weekend",
        "Sorted everything by color and season - took longer than expected "
        "but I can actually find things now. Anyone have a system that's "
        "held up long term?",
        is_anonymous=False,
    ),
    SeedPost(
        "shoe_matching_struggle",
        "mia",
        "Why is matching shoes to an outfit so hard for me",
        "I'll plan a whole outfit and then just stare at my shoe rack for "
        "ten minutes. Curious how other people think about this.",
        is_anonymous=True,
    ),
    SeedPost(
        "photo_before_wash",
        "leo",
        "Habit that's saved me a few times: photo before the wash",
        "If an outfit came together well, I snap a quick photo before it "
        "goes back into rotation and gets split up in laundry.",
        is_anonymous=False,
    ),
    SeedPost(
        "capsule_for_work",
        "sam",
        "Built a small capsule wardrobe for work and it's holding up",
        "About a dozen pieces on rotation for two months now and I still "
        "don't feel like I'm repeating myself. Happy to share the list if "
        "useful.",
        is_anonymous=True,
    ),
    SeedPost(
        "color_clash_feature",
        "nina",
        "Would love smarter color-clash warnings in recommendations",
        "Right now it mostly matches by season/occasion - flagging when "
        "two pieces actually clash colorwise would help a lot. Anyone "
        "else want this?",
        is_anonymous=False,
    ),
    SeedPost(
        "outfit_builder_praise",
        "jordan",
        "The outfit builder has genuinely changed how I get dressed",
        "Being able to put a full look together from stuff I already own "
        "and save it for later is such a small thing that ended up "
        "mattering a lot.",
        is_anonymous=False,
    ),
]

PROD_SEED_COMMENTS = [
    SeedComment(
        "closet_reply_mia",
        "weekend_closet_reset",
        "mia",
        "This is motivating me to finally do the same thing.",
    ),
    SeedComment(
        "closet_reply_leo",
        "weekend_closet_reset",
        "leo",
        "Color sorting was the move for me too, way easier to find things.",
    ),
    SeedComment(
        "photo_reply_nina",
        "photo_before_wash",
        "nina",
        "Stealing this habit starting today.",
    ),
    SeedComment(
        "capsule_reply_jordan",
        "capsule_for_work",
        "jordan",
        "Would love to see the full list if you're willing to share.",
    ),
    SeedComment(
        "color_reply_sam",
        "color_clash_feature",
        "sam",
        "Yes, this would genuinely help me a lot.",
    ),
]

PROD_SEED_REACTIONS = [
    SeedReaction("react_closet_leo", "post", "weekend_closet_reset", "leo", 1),
    SeedReaction("react_closet_sam", "post", "weekend_closet_reset", "sam", 1),
    SeedReaction("react_shoe_nina", "post", "shoe_matching_struggle", "nina", -1),
    SeedReaction("react_photo_jordan", "post", "photo_before_wash", "jordan", 1),
    SeedReaction("react_capsule_mia", "post", "capsule_for_work", "mia", 1),
    SeedReaction("react_color_leo", "post", "color_clash_feature", "leo", 1),
    SeedReaction("react_comment_mia", "comment", "closet_reply_mia", "jordan", 1),
    SeedReaction("react_comment_jordan", "comment", "capsule_reply_jordan", "nina", -1),
]


@dataclass
class SeedProfile:
    name: str
    users: list[SeedUser]
    posts: list[SeedPost]
    comments: list[SeedComment]
    reactions: list[SeedReaction]


PROFILES: dict[str, SeedProfile] = {
    "demo": SeedProfile("demo", DEMO_SEED_USERS, DEMO_SEED_POSTS, DEMO_SEED_COMMENTS, DEMO_SEED_REACTIONS),
    "production": SeedProfile(
        "production", PROD_SEED_USERS, PROD_SEED_POSTS, PROD_SEED_COMMENTS, PROD_SEED_REACTIONS
    ),
}


def _resolve_password(profile_name: str) -> str:
    """Returns the seed password for the given profile, failing loudly
    rather than falling back to any default - see the module docstring's
    "Profiles" section."""
    if profile_name == "demo":
        return _DEMO_PASSWORD
    if profile_name == "production":
        password = os.environ.get(_PROD_SEED_PASSWORD_ENV_VAR)
        if not password:
            raise SeedError(
                f"--profile production requires the {_PROD_SEED_PASSWORD_ENV_VAR} "
                "environment variable to be set. This is intentional - the "
                "production seed password is never hardcoded in this script "
                "and never committed to git. Provision it out-of-band (e.g. "
                "in shared/.env on the production host, the same way "
                "JWT_SECRET/MONGO_ROOT_PASSWORD already are - see "
                "docs/deployment.md's \"Production cold-seed\" section) and "
                "re-run."
            )
        return password
    raise SeedError(f"Unknown seed profile {profile_name!r}")


# Every comment must come from someone other than the real author of the
# post it's on - enforced structurally here (not just by eyeballing the
# lists above), for every profile, so a future edit to any of these lists
# can't silently violate it.
for _profile in PROFILES.values():
    _post_author_by_key = {p.key: p.author_key for p in _profile.posts}
    for _c in _profile.comments:
        if _c.author_key == _post_author_by_key[_c.post_key]:
            raise SeedError(
                f"Seed data error: profile {_profile.name!r} comment "
                f"{_c.key!r} is authored by the same user as its own "
                f"target post {_c.post_key!r}"
            )


# --- Users: register-or-login (see "Idempotency" in the module docstring) ---


async def ensure_user(
    client: httpx.AsyncClient, user: SeedUser, password: str
) -> tuple[str, bool]:
    """Register (if absent) then log in; return (bearer_token, was_newly_created).

    POST /auth/register returns UserPublic (username/email only - verified
    against services/web/app/routers/auth.py) - it never returns a token,
    so a login call is always required, not just on the 409 branch.

    The mandatory login-after-409 also doubles as a collision guard: if a
    real user already independently registered this exact username with a
    *different* password, login fails here (not 200) and the run stops via
    SeedError below rather than silently proceeding - it can never take
    over or corrupt a real account.
    """
    register_resp = await _request_with_retry(
        client,
        "POST",
        "/auth/register",
        json={"username": user.username, "email": user.email, "password": password},
    )
    if register_resp.status_code == 201:
        created = True
    elif register_resp.status_code == 409:
        created = False
    else:
        raise SeedError(
            f"POST /auth/register for {user.username!r} failed: "
            f"{register_resp.status_code} {register_resp.text[:200]}"
        )

    login_resp = await _request_with_retry(
        client, "POST", "/auth/login", json={"username": user.username, "password": password}
    )
    if login_resp.status_code != 200:
        raise SeedError(
            f"POST /auth/login for {user.username!r} failed: "
            f"{login_resp.status_code} {login_resp.text[:200]}"
        )
    status = "registered" if created else "already existed"
    print(f"  [{user.username}] {status}, logged in")
    return login_resp.json()["access_token"], created


# --- Paginated GET helper - always walks every page, never assumes page 1
# is the whole collection (matters once real users have posted too). ---


async def _get_all_pages(
    client: httpx.AsyncClient, url: str, headers: dict, page_size: int = 100
) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        resp = await _request_with_retry(
            client, "GET", url, headers=headers, params={"page": page, "page_size": page_size}
        )
        if resp.status_code != 200:
            raise SeedError(f"GET {url} (page {page}) failed: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        items.extend(body["items"])
        if not body["has_more"]:
            return items
        page += 1


# --- Posts: check-then-create, individually per post (never a global
# "does the first one exist" shortcut - see module docstring). ---


async def ensure_post(client: httpx.AsyncClient, token: str, post: SeedPost) -> tuple[dict, bool]:
    """Returns (post_doc, was_newly_created)."""
    headers = {"Authorization": f"Bearer {token}"}
    mine = await _get_all_pages(client, "/posts/mine", headers)
    for existing in mine:
        if existing["title"] == post.title:
            print(f"  [post:{post.key}] already exists (id={existing['id']})")
            return existing, False

    resp = await _request_with_retry(
        client,
        "POST",
        "/posts",
        headers=headers,
        data={"title": post.title, "body": post.body, "is_anonymous": post.is_anonymous},
    )
    if resp.status_code != 201:
        raise SeedError(
            f"POST /posts for {post.key!r} failed: {resp.status_code} {resp.text[:200]}"
        )
    created = resp.json()
    print(f"  [post:{post.key}] created (id={created['id']})")
    return created, True


# --- Comments: check-then-create, individually per comment, matched by
# (author_username, body) within the target post's own comment list -
# comments are never anonymous, so author_username is always present. ---


async def ensure_comment(
    client: httpx.AsyncClient, token: str, author_username: str, post_id: str, comment: SeedComment
) -> tuple[dict, bool]:
    """Returns (comment_doc, was_newly_created)."""
    headers = {"Authorization": f"Bearer {token}"}
    existing_comments = await _get_all_pages(client, f"/posts/{post_id}/comments", headers)
    for existing in existing_comments:
        if existing["author_username"] == author_username and existing["body"] == comment.body:
            print(f"  [comment:{comment.key}] already exists (id={existing['id']})")
            return existing, False

    resp = await _request_with_retry(
        client, "POST", f"/posts/{post_id}/comments", headers=headers, data={"body": comment.body}
    )
    if resp.status_code != 201:
        raise SeedError(
            f"POST /posts/{post_id}/comments for {comment.key!r} failed: "
            f"{resp.status_code} {resp.text[:200]}"
        )
    created = resp.json()
    print(f"  [comment:{comment.key}] created (id={created['id']})")
    return created, True


# --- Reactions: reconcile to a target state, never blindly re-POST (it's a
# toggle - re-sending an already-correct value would remove it). ---


async def ensure_reaction(
    client: httpx.AsyncClient,
    token: str,
    reaction: SeedReaction,
    target_id: str,
    comment_post_id: str | None,
) -> str:
    headers = {"Authorization": f"Bearer {token}"}

    if reaction.target_kind == "post":
        listing = await _get_all_pages(client, "/posts", headers)
        current = next((p["my_reaction"] for p in listing if p["id"] == target_id), None)
        react_url = f"/posts/{target_id}/reactions"
    else:
        assert comment_post_id is not None
        listing = await _get_all_pages(client, f"/posts/{comment_post_id}/comments", headers)
        current = next((c["my_reaction"] for c in listing if c["id"] == target_id), None)
        react_url = f"/comments/{target_id}/reactions"

    if current == reaction.value:
        print(f"  [reaction:{reaction.key}] already correct (my_reaction={current}) - unchanged")
        return "unchanged"

    resp = await _request_with_retry(
        client, "POST", react_url, headers=headers, json={"value": reaction.value}
    )
    if resp.status_code != 200:
        raise SeedError(
            f"POST {react_url} for {reaction.key!r} failed: {resp.status_code} {resp.text[:200]}"
        )
    print(f"  [reaction:{reaction.key}] applied (was {current}, now {reaction.value})")
    return "applied"


async def _wait_for_web(client: httpx.AsyncClient, attempts: int = 10, delay: float = 3.0) -> bool:
    """docker compose up -d returns before `web` finishes its MongoDB-
    healthy dependency wait - retry GET /health for a bounded window rather
    than failing on the very first attempt."""
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.get("/health", timeout=5.0)
            if resp.status_code == 200:
                return True
            print(f"  web reachability check: unexpected status {resp.status_code}")
        except httpx.HTTPError as exc:
            print(f"  web reachability check: not yet reachable ({exc})")
        if attempt < attempts:
            await asyncio.sleep(delay)
    return False


@dataclass
class RunSummary:
    users_created: int = 0
    users_existing: int = 0
    posts_created: int = 0
    posts_existing: int = 0
    comments_created: int = 0
    comments_existing: int = 0
    reactions_applied: int = 0
    reactions_unchanged: int = 0


async def run(base_url: str, profile_name: str = "demo") -> RunSummary:
    if profile_name not in PROFILES:
        raise SeedError(
            f"Unknown --profile {profile_name!r}; expected one of {sorted(PROFILES)}"
        )
    # Resolved (and, for "production", validated) before any network call -
    # a missing PROD_SEED_PASSWORD must fail loudly with nothing attempted,
    # not partway through a run.
    password = _resolve_password(profile_name)
    profile = PROFILES[profile_name]

    summary = RunSummary()

    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        print(f"Smart Wardrobe cold-seed - profile {profile_name!r} - target {base_url}")

        print("Checking web service reachability (GET /health)...")
        if not await _wait_for_web(client):
            raise SeedError(
                f"web service not reachable at {base_url} after retrying. "
                "Start the stack first (`docker compose up -d`) and confirm it's healthy."
            )
        print("  web is reachable.")

        # --- Users ---
        print(f"\n=== Users ({len(profile.users)}) ===")
        tokens: dict[str, str] = {}
        for user in profile.users:
            token, created = await ensure_user(client, user, password)
            tokens[user.key] = token
            if created:
                summary.users_created += 1
            else:
                summary.users_existing += 1

        # --- Posts ---
        print(f"\n=== Posts ({len(profile.posts)}) ===")
        post_ids: dict[str, str] = {}
        for post in profile.posts:
            doc, created = await ensure_post(client, tokens[post.author_key], post)
            post_ids[post.key] = doc["id"]
            if created:
                summary.posts_created += 1
            else:
                summary.posts_existing += 1

        # --- Comments ---
        print(f"\n=== Comments ({len(profile.comments)}) ===")
        comment_ids: dict[str, str] = {}
        username_by_key = {u.key: u.username for u in profile.users}
        for comment in profile.comments:
            doc, created = await ensure_comment(
                client,
                tokens[comment.author_key],
                username_by_key[comment.author_key],
                post_ids[comment.post_key],
                comment,
            )
            comment_ids[comment.key] = doc["id"]
            if created:
                summary.comments_created += 1
            else:
                summary.comments_existing += 1

        # --- Reactions ---
        print(f"\n=== Reactions ({len(profile.reactions)}) ===")
        comment_post_id = {c.key: post_ids[c.post_key] for c in profile.comments}
        for reaction in profile.reactions:
            token = tokens[reaction.reactor_key]
            if reaction.target_kind == "post":
                target_id = post_ids[reaction.target_key]
                outcome = await ensure_reaction(client, token, reaction, target_id, None)
            else:
                target_id = comment_ids[reaction.target_key]
                outcome = await ensure_reaction(
                    client, token, reaction, target_id, comment_post_id[reaction.target_key]
                )
            if outcome == "applied":
                summary.reactions_applied += 1
            else:
                summary.reactions_unchanged += 1

    return summary


def _print_summary(summary: RunSummary) -> None:
    print("\n=== Seed summary ===")
    print(f"  users:     {summary.users_created} created, {summary.users_existing} already existed")
    print(f"  posts:     {summary.posts_created} created, {summary.posts_existing} already existed")
    print(f"  comments:  {summary.comments_created} created, {summary.comments_existing} already existed")
    print(
        f"  reactions: {summary.reactions_applied} applied, "
        f"{summary.reactions_unchanged} already correct (unchanged)"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Smart Wardrobe API base URL (default: http://localhost:8000, "
        "correct when run inside the `web` container via `docker compose exec` "
        "- see the module docstring)",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default="demo",
        help="Which fixed seed dataset to run (default: demo). 'production' "
        "requires PROD_SEED_PASSWORD to be set and is never run "
        "automatically - see the module docstring's \"Profiles\" section "
        "and docs/deployment.md.",
    )
    args = parser.parse_args()

    start = time.monotonic()
    try:
        summary = await run(args.base_url, args.profile)
    except SeedError as exc:
        print(f"\nSeed run FAILED: {exc}", file=sys.stderr)
        print(
            "The script can be safely re-run - every object is checked "
            "individually before being created, so re-running will not "
            "duplicate anything already seeded.",
            file=sys.stderr,
        )
        return 1

    elapsed = time.monotonic() - start
    _print_summary(summary)
    print(f"\nDone in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
