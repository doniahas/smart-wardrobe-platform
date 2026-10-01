"""UI browser-navigation fix: lightweight content assertions against the
exact static file FastAPI serves under `/ui/` (`app/static/explore-core.js`,
`app/static/index.html`) - same style as test_frontend_ui_redesign.py /
test_logout_auth_state.py, not a JS test run (there is no frontend/JS
test-execution tooling in this project - see docs/testing.md's "Known
gaps"). These prove the shipped source actually contains the History-API
navigation logic described in docs/architecture.md's "Frontend" section,
not just that the app "looks right".

Root cause this guards against: section switches (showDashboard/
showExploreTab/showMessagesTab/showChatTab) only ever toggled `.hidden`
classes - no browser history entry was ever created, so Back left the app
entirely instead of restoring the previous section, and a refresh always
reset to Dashboard regardless of which section was open.
"""


def _extract_function_body(js: str, function_name: str) -> str:
    """Returns the source of one top-level `function NAME(...) { ... }` by
    brace-matching from its opening `{` - same helper as
    test_logout_auth_state.py, duplicated locally (that file's is a
    module-private helper, not a shared import) rather than adding a new
    shared test-utility module for one function."""
    start = js.index(f"function {function_name}(")
    brace_start = js.index("{", start)
    depth = 0
    i = brace_start
    while i < len(js):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[brace_start : i + 1]
        i += 1
    raise AssertionError(f"Could not find a matching closing brace for function {function_name!r}")


def _get_explore_core_js(client) -> str:
    resp = client.get("/ui/explore-core.js")
    assert resp.status_code == 200
    return resp.text


# --- A/D/E: tabFromHash() - the fallback/validation logic ─────────────


def test_nav_tabs_are_exactly_the_four_top_level_sections(client):
    js = _get_explore_core_js(client)
    assert "const NAV_TABS = ['dashboard', 'explore', 'messages', 'chat'];" in js


def test_tab_from_hash_falls_back_to_dashboard_for_unknown_or_empty_hash(client):
    """D + E: plain /ui/ (empty hash) and any invalid/unrecognized hash
    value must both safely resolve to 'dashboard' - never throw, never
    resolve to something not in NAV_TABS."""
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "tabFromHash")
    assert "NAV_TABS.includes(raw) ? raw : 'dashboard'" in body


def test_activate_tab_dispatches_every_real_section_by_name(client):
    """E: a direct authenticated deep link for each real top-level section
    (/ui/#dashboard, /ui/#explore, /ui/#messages, /ui/#chat) must restore
    that exact section - activateTab() is the single dispatcher both
    initExplore() (boot/login) and the popstate handler use to turn a
    hash-derived tab name into the right show*Tab() call."""
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "activateTab")
    for tab, fn_name in [
        ("dashboard", "showDashboard"),
        ("explore", "showExploreTab"),
        ("messages", "showMessagesTab"),
        ("chat", "showChatTab"),
    ]:
        assert f"tab === '{tab}') {fn_name}(mode)" in body, (
            f"activateTab() must dispatch '{tab}' to {fn_name}(mode)"
        )


# --- A: clicking a nav item updates navigation state ───────────────────


def test_each_top_level_show_function_syncs_its_own_tab_name_to_history(client):
    js = _get_explore_core_js(client)
    for fn_name, tab in [
        ("showDashboard", "dashboard"),
        ("showExploreTab", "explore"),
        ("showMessagesTab", "messages"),
        ("showChatTab", "chat"),
    ]:
        body = _extract_function_body(js, fn_name)
        assert f"syncTabHistory('{tab}', historyMode)" in body, (
            f"{fn_name}() must call syncTabHistory('{tab}', historyMode)"
        )


def test_nav_button_onclick_handlers_are_unchanged_zero_arg_calls(client):
    """Existing nav buttons must keep calling showDashboard()/
    showExploreTab()/showMessagesTab()/showChatTab() with no arguments -
    the new historyMode parameter defaults to 'push' precisely so no call
    site needs to change. This also protects
    test_frontend_ui_redesign.py::test_every_existing_nav_tab_still_reachable
    and test_chat_frontend_route.py's literal 'showChatTab()' assertions
    from silently regressing together with this file."""
    resp = client.get("/ui/index.html")
    assert resp.status_code == 200
    html = resp.text
    for nav_id, handler in [
        ("nav-dashboard", 'onclick="showDashboard()"'),
        ("nav-explore", 'onclick="showExploreTab()"'),
        ("nav-messages", 'onclick="showMessagesTab()"'),
        ("nav-chat", 'onclick="showChatTab()"'),
    ]:
        assert f'id="{nav_id}"' in html
        assert handler in html


def test_show_functions_default_history_mode_to_push(client):
    js = _get_explore_core_js(client)
    for fn_name in ("showDashboard", "showExploreTab", "showMessagesTab", "showChatTab"):
        assert f"function {fn_name}(historyMode = 'push')" in js


# --- B: Back/Forward restore sections via popstate ─────────────────────


def test_popstate_listener_is_registered_exactly_once(client):
    """G: must never be duplicated - this file is only ever loaded once
    per real page load (see its own header comment), so exactly one
    `addEventListener('popstate', ...)` call must exist in the source."""
    js = _get_explore_core_js(client)
    assert js.count("addEventListener('popstate'") == 1


def test_popstate_handler_restores_the_hash_tab_without_rewriting_history(client):
    """B + G: Back/Forward must re-activate whatever the URL's fragment
    now names, using historyMode 'none' so the handler itself never calls
    pushState/replaceState again - doing so would fight the browser's own
    traversal and could corrupt the Back/Forward stack."""
    js = _get_explore_core_js(client)
    popstate_start = js.index("addEventListener('popstate'")
    # The handler is a short arrow-function callback ending at the
    # addEventListener call's closing `);` - slice a generous window
    # rather than brace-matching an anonymous function.
    handler_src = js[popstate_start : popstate_start + 300]
    assert "activateTab(tabFromHash(), 'none')" in handler_src


def test_popstate_handler_is_guarded_on_auth_state(client):
    """H: while logged out, showAuth() is the sole authority on what's
    visible - a stale or foreign hash must never re-show an authenticated
    container out from under it via a popstate event."""
    js = _get_explore_core_js(client)
    popstate_start = js.index("addEventListener('popstate'")
    handler_src = js[popstate_start : popstate_start + 300]
    assert "if (!getToken()) return;" in handler_src


# --- C: reload/direct-open restores the correct section ────────────────


def test_init_explore_restores_from_hash_instead_of_forcing_dashboard(client):
    """C + E: initExplore() (called by both boot() and the login handler
    in app.js) must activate whatever tabFromHash() resolves to, using
    'replace' so restoring on load never adds an extra Back-stop before
    the first section actually seen."""
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "initExplore")
    assert "activateTab(tabFromHash(), 'replace')" in body
    # It must no longer unconditionally force the dashboard tab.
    assert "showTab('dashboard')" not in body


def test_init_explore_resets_explore_loaded_before_restoring_the_tab(client):
    """F: if the restored tab is 'explore', showExploreTab()'s own
    if(!exploreLoaded) gate must see 'not yet loaded' so the initial
    feed/my-posts/outfit-picker fetches actually fire - this only works
    if exploreLoaded is reset *before* activateTab() runs, not after."""
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "initExplore")
    reset_index = body.index("exploreLoaded = false;")
    activate_index = body.index("activateTab(tabFromHash(), 'replace')")
    assert reset_index < activate_index


# --- F: section-specific data loading still occurs ──────────────────────


def test_show_explore_tab_still_lazy_loads_feed_my_posts_and_outfit_picker(client):
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "showExploreTab")
    assert "if (!exploreLoaded)" in body
    for call in ("loadFeed();", "loadMyPosts();", "loadOutfitsForPicker();"):
        assert call in body


def test_show_messages_tab_still_loads_inbox(client):
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "showMessagesTab")
    assert "loadInbox();" in body


def test_show_chat_tab_still_shows_recommendations_subtab(client):
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "showChatTab")
    assert "showAiStylistSubtab('recommendations');" in body


def test_show_dashboard_still_shows_overview_subtab(client):
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "showDashboard")
    assert "showDashboardSubtab('overview');" in body


# --- G: no duplicated init/requests on Back/Forward ─────────────────────


def test_explore_loaded_guard_is_unchanged_so_back_forward_never_reloads_data(client):
    """Navigating Explore -> elsewhere -> back to Explore (whether by nav
    click or Back/Forward) must not re-fetch the feed/my-posts/outfit
    picker a second time in the same session - only initExplore() (a
    fresh boot/login) resets exploreLoaded back to false."""
    js = _get_explore_core_js(client)
    # module-level default + initExplore() (fresh boot/login) + the
    # pre-existing teardownExplore() reset on logout - never inside a
    # show*Tab()/popstate/Back-Forward path.
    assert js.count("exploreLoaded = false;") == 3
    assert js.count("exploreLoaded = true;") == 1  # only inside showExploreTab()'s gate


# --- H: logout/auth behavior is unaffected, and logout resets the ───────
#     canonical navigation state so a later login doesn't inherit it ─────


def test_teardown_explore_still_shows_dashboard_via_raw_show_tab(client):
    """H: logout must not be broken by the new navigation state -
    teardownExplore() (called from app.js's logout(), before showAuth())
    must keep calling the raw showTab('dashboard') - not showDashboard() -
    so this DOM reset itself never pushes a *new* history entry (it still
    resets the existing entry to the canonical default - see the test
    below - just never via the push-capable showDashboard()/
    syncTabHistory() path). (showAuth() itself, called after
    teardownExplore(), remains the authoritative hide-everything step -
    unchanged, and unrelated to this file; see test_logout_auth_state.py.)"""
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "teardownExplore")
    assert "showTab('dashboard');" in body
    assert "showDashboard(" not in body
    # A genuine *call* (not merely a comment mentioning it by name, which
    # this function's own explanatory comment does) must not appear -
    # the history reset below is a direct, unconditional replaceState,
    # deliberately bypassing syncTabHistory()'s push/replace/none
    # branching entirely.
    assert "syncTabHistory(" not in body


def test_teardown_explore_resets_navigation_state_to_dashboard_on_logout(client):
    """Logout must leave the address bar / history state at the canonical
    default ('dashboard'), not whatever authenticated section (e.g.
    #messages) happened to be open before Sign Out - otherwise a normal
    login right afterward in the same tab would silently reopen that
    stale section instead of Dashboard (see initExplore(), which reads
    exactly this state on every login). Uses replaceState directly
    (unconditionally, not through syncTabHistory's dedup branching) so
    logout never adds an extra Back-stop of its own - it only resets what
    is already there."""
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "teardownExplore")
    assert "history.replaceState({ tab: 'dashboard' }, '', '#dashboard');" in body
    assert "currentHistoryTab = 'dashboard';" in body
    # The reset must happen after the DOM has already been switched back
    # to the dashboard container, not before.
    assert body.index("showTab('dashboard');") < body.index("history.replaceState(")


def test_init_explore_still_restores_from_whatever_hash_is_present_at_login(client):
    """The logout fix works specifically *because* initExplore() (the
    login path) keeps reading the current hash unconditionally - logout
    is what changed (now resets it to #dashboard first), not login.
    This guards against a regression where the reset gets implemented by
    special-casing login/boot() instead, which would also break restoring
    a genuine direct deep link (e.g. /ui/#explore opened fresh, or a
    session-expired -> re-login round trip on that same URL)."""
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "initExplore")
    assert "activateTab(tabFromHash(), 'replace')" in body


# --- Existing behavior not broken: goToRecommendationForm() quick action ─


def test_go_to_recommendation_form_still_navigates_and_scrolls(client):
    js = _get_explore_core_js(client)
    body = _extract_function_body(js, "goToRecommendationForm")
    assert "showChatTab();" in body
    assert "getElementById('r-occasion').scrollIntoView(" in body
