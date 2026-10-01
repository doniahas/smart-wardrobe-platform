from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PageParams:
    """Shared page/page_size query params for list endpoints.

    Introduced with forum posts (routers/posts.py) - the first paginated list
    endpoints in this codebase - and meant to be reused as-is by every later
    paginated endpoint (comments, direct messages, notifications) instead of
    each one re-implementing skip/limit parsing and clamping.
    """

    def __init__(
        self,
        page: int = Query(1, ge=1),
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    ):
        self.page = page
        self.page_size = page_size

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size


class Page(BaseModel, Generic[T]):
    """A single page of results.

    `has_more` is computed by callers fetching `page_size + 1` documents
    (see routers/posts.py) rather than a separate count_documents call, so
    pagination stays cheap on large collections.
    """

    items: list[T]
    page: int
    page_size: int
    has_more: bool
