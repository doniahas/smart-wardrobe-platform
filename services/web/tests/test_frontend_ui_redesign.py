"""UI redesign: lightweight content assertions
against the exact static files FastAPI serves under `/ui/` - same style as
test_root_redirect.py/test_chat_frontend_route.py, not a JS test run (see
docs/testing.md's "Known gaps"). Covers the two claims that are easy to
silently regress on a future edit: no external font network dependency,
and the dashboard overview stats actually exist with the ids app.js writes
real counts into.
"""


def test_served_index_html_has_no_external_font_dependency(client):
    """The redesign replaced the Google Fonts webfont with a system-font
    stack specifically so typography never depends on network access -
    this proves the served page has no such link left in it."""
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    html = resp.text
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html


def test_served_style_css_defines_system_font_stacks_only(client):
    resp = client.get("/ui/style.css")
    assert resp.status_code == 200
    css = resp.text
    assert "fonts.googleapis.com" not in css
    # The two families the redesign settled on.
    assert "--font-serif:" in css
    assert "--font-sans:" in css
    assert "Georgia" in css


def test_dashboard_overview_stat_elements_exist(client):
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="stat-wardrobe-count"' in html
    assert 'id="stat-outfits-count"' in html


def test_app_js_writes_real_counts_into_the_overview_stats(client):
    """Proves the counts come from data already fetched (wardrobe/outfits
    list length), not a hardcoded or fabricated value."""
    resp = client.get("/ui/app.js")
    assert resp.status_code == 200
    js = resp.text
    assert "stat-wardrobe-count').textContent = items.length" in js
    assert "stat-outfits-count').textContent = outfits.length" in js


def test_every_existing_nav_tab_still_reachable(client):
    """The redesign must not have dropped access to any existing top-level
    surface while restyling the nav."""
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    html = resp.text
    for nav_id, handler in [
        ("nav-dashboard", "showDashboard()"),
        ("nav-explore", "showExploreTab()"),
        ("nav-messages", "showMessagesTab()"),
        ("nav-chat", "showChatTab()"),
    ]:
        assert f'id="{nav_id}"' in html
        assert handler in html
