import re

from fastapi import APIRouter, Depends, Query

from app.auth import get_current_user
from app.core.database import get_db
from app.core.pagination import Page, PageParams
from app.models import UserSummaryPublic

router = APIRouter(prefix="/users", tags=["users"])

# Bounds how much of a search term is ever matched against - a search box
# is user-facing free text, not a trusted identifier, so it's clamped the
# same way every other user-supplied string in this app is (see
# routers/posts.py's _POST_TITLE_MAX_LENGTH-style constants) rather than
# passed through unbounded.
_SEARCH_MAX_LENGTH = 50


@router.get("", response_model=Page[UserSummaryPublic])
async def list_users(
    search: str | None = Query(default=None, max_length=_SEARCH_MAX_LENGTH),
    pagination: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Authenticated, minimal user directory - exists only to let a client
    start a *new* direct message with someone they have no other way to
    find (see models.py::UserSummaryPublic's module note for the full
    reasoning).

    Returns only `{id, username}` per user, via an explicit Mongo
    projection (never the full user document) - no email, password hash,
    memory/preferences, wardrobe data, or any other field ever leaves this
    route. The caller themself is always excluded. `search`, when given,
    is a plain case-insensitive substring match against `username` only -
    the search term is regex-escaped before use (`re.escape`), so it can
    never be interpreted as a user-supplied regex/query operator; there is
    no way to pass raw Mongo query syntax through this parameter. Results
    are paginated through the same `PageParams`/`Page` machinery every
    other list endpoint in this app already uses (bounded page size,
    `MAX_PAGE_SIZE`-clamped), so this can never return an unbounded
    result set regardless of how many accounts exist.

    Deliberately does not use/expose/relate to anonymous-post identity in
    any way: this route only ever reads app.users (real account
    usernames), never app.posts, and a post's `is_anonymous`
    author-hiding (routers/posts.py::_doc_to_public) is completely
    independent of - and unaffected by - this route existing.
    """
    current_user_id = current_user["_id"]
    query: dict = {"_id": {"$ne": current_user_id}}
    if search:
        query["username"] = {"$regex": re.escape(search), "$options": "i"}

    cursor = (
        db.users.find(query, {"username": 1})
        .sort([("username", 1), ("_id", 1)])
        .skip(pagination.skip)
        .limit(pagination.page_size + 1)
    )
    docs = [doc async for doc in cursor]
    has_more = len(docs) > pagination.page_size
    docs = docs[: pagination.page_size]

    items = [UserSummaryPublic(id=str(doc["_id"]), username=doc["username"]) for doc in docs]
    return Page[UserSummaryPublic](
        items=items, page=pagination.page, page_size=pagination.page_size, has_more=has_more
    )
