from app.core.pagination import MAX_PAGE_SIZE, PageParams


def test_skip_is_zero_on_first_page():
    params = PageParams(page=1, page_size=20)
    assert params.skip == 0


def test_skip_advances_by_page_size():
    params = PageParams(page=3, page_size=10)
    assert params.skip == 20


def test_max_page_size_constant_is_enforced_by_query_bound():
    """core/pagination.py's Query(..., le=MAX_PAGE_SIZE) is what actually
    enforces this at the HTTP layer (see test_posts_route.py for the
    end-to-end 422 case) - this just guards the constant itself from
    silently drifting to something unreasonable."""
    assert MAX_PAGE_SIZE == 100
