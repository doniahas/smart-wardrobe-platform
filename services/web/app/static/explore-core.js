/* ── Explore: shared status helpers + nav/init orchestration ───────
   Loaded first among the explore-*.js files (order otherwise doesn't
   matter - none of them run top-level code that depends on another,
   only on the DOM already being parsed, same as app.js). Must load
   before app.js, since app.js's boot()/login handlers call
   initExplore()/disconnectWebSocket() defined here. ─────────────── */

let exploreLoaded = false;

/* ── Browser history integration (UI browser-navigation fix) ─────────
   Gives the browser real navigation/history state for the app's four
   top-level sections - exactly showTab()'s own four values, each with
   its own dedicated nav button (#nav-dashboard/explore/messages/chat)
   and top-level content container (#app/#explore-app/#messages-app/
   #chat-app). Deliberately does NOT extend to subtabs (dashboard
   overview/actions, explore feed/my-posts/search-people, AI stylist
   chat/recommendations) or anything else transient (modals, the
   notification panel, dropdowns, form state) - those stay exactly as
   before, toggled in place with no history entry, matching how the app
   already treats "section" as one-of-four-top-level-containers and
   everything else as view state within one.

   Mechanism: the URL fragment (#dashboard/#explore/#messages/#chat)
   plus the History API. No backend route changes - `/ui/` still serves
   the same static index.html for every path under it (see main.py's
   StaticFiles mount), and the fragment is never sent to the server, so
   a refresh re-requests plain `/ui/` and the browser re-applies the
   fragment client-side once the page loads - boot() below just has to
   read it back. */

const NAV_TABS = ['dashboard', 'explore', 'messages', 'chat'];

function tabFromHash() {
  const raw = (location.hash || '').slice(1);
  return NAV_TABS.includes(raw) ? raw : 'dashboard';
}

// Tracks whatever tab the *current* history entry already reflects, so a
// repeat call for the same tab (e.g. clicking an already-active nav
// button) never pushes a redundant entry. Updated on every navigation,
// including ones that don't touch history (see syncTabHistory's 'none'
// mode, used when a popstate event means the browser already did it).
let currentHistoryTab = null;

/* mode: 'push' (default - a real, user-initiated navigation: click a
   nav button, or an internal quick-action like goToRecommendationForm),
   'replace' (restoring state without adding a new entry - initial
   load/login, since that shouldn't leave an extra Back-stop before the
   very first section the user actually saw), or 'none' (a popstate
   handler already reflects a browser-driven history change - just
   record it, never re-touch history for it, or Back/Forward would fight
   the browser's own traversal). */
function syncTabHistory(tab, mode) {
  if (mode === 'none') { currentHistoryTab = tab; return; }
  if (mode === 'replace' || currentHistoryTab === null) {
    history.replaceState({ tab }, '', `#${tab}`);
  } else if (tab !== currentHistoryTab) {
    history.pushState({ tab }, '', `#${tab}`);
  }
  currentHistoryTab = tab;
}

// Dispatches to the right show*Tab() function for a given tab name -
// used by initExplore() (restoring on boot/login) and the popstate
// listener below, both of which need to activate an arbitrary tab by
// name rather than calling one specific show*Tab() directly.
function activateTab(tab, mode) {
  if (tab === 'dashboard') showDashboard(mode);
  else if (tab === 'explore') showExploreTab(mode);
  else if (tab === 'messages') showMessagesTab(mode);
  else if (tab === 'chat') showChatTab(mode);
}

// Registered once at script load (this file loads once per real page
// load - see the header comment - so this listener is never duplicated
// across internal Back/Forward navigation, only across a genuine full
// reload). Guarded on getToken(): while logged out, showAuth() is the
// sole authority on what's visible (see app.js), so a stale/foreign hash
// must never re-show an authenticated container underneath it.
window.addEventListener('popstate', () => {
  if (!getToken()) return;
  activateTab(tabFromHash(), 'none');
});

/* ── Loading / empty / error list-state helpers ─────────────────────
   Extends the app's existing 2-state (grid + empty) convention to a
   3rd state (loading/error), reusing the existing .hint and .msg err
   classes rather than inventing new ones. */

function setLoading(id, text) {
  const el = document.getElementById(id);
  el.className = 'hint';
  el.textContent = text;
}

function clearStatus(id) {
  const el = document.getElementById(id);
  el.className = 'hint';
  el.textContent = '';
}

function setError(id, text, retryFn) {
  const el = document.getElementById(id);
  el.className = 'msg err';
  el.textContent = '';
  el.append(text + ' ');
  const retryBtn = document.createElement('button');
  retryBtn.type = 'button';
  retryBtn.className = 'btn btn-ghost btn-sm';
  retryBtn.textContent = 'Retry';
  retryBtn.addEventListener('click', retryFn);
  el.appendChild(retryBtn);
}

/* ── Tab navigation (Dashboard / Explore / Messages / AI Stylist) ── */

function showTab(tab) {
  document.getElementById('app').classList.toggle('hidden', tab !== 'dashboard');
  document.getElementById('explore-app').classList.toggle('hidden', tab !== 'explore');
  document.getElementById('messages-app').classList.toggle('hidden', tab !== 'messages');
  document.getElementById('chat-app').classList.toggle('hidden', tab !== 'chat');
  document.getElementById('nav-dashboard').classList.toggle('active', tab === 'dashboard');
  document.getElementById('nav-explore').classList.toggle('active', tab === 'explore');
  document.getElementById('nav-messages').classList.toggle('active', tab === 'messages');
  document.getElementById('nav-chat').classList.toggle('active', tab === 'chat');
}

function showDashboard(historyMode = 'push') {
  showTab('dashboard');
  showDashboardSubtab('overview');
  syncTabHistory('dashboard', historyMode);
}

function showExploreTab(historyMode = 'push') {
  showTab('explore');
  showExploreSubtab('feed');
  if (!exploreLoaded) {
    exploreLoaded = true;
    loadFeed();
    loadMyPosts();
    loadOutfitsForPicker();
  }
  syncTabHistory('explore', historyMode);
}

function showMessagesTab(historyMode = 'push') {
  showTab('messages');
  loadInbox();
  syncTabHistory('messages', historyMode);
}

function showChatTab(historyMode = 'push') {
  showTab('chat');
  // Recommendations is the default AI Stylist subview; Chat remains
  // one click away via the subtab button. Matches index.html's own default
  // active/hidden classes on the two subtab elements below.
  showAiStylistSubtab('recommendations');
  syncTabHistory('chat', historyMode);
}

/* ── Subtabs ───────────────────────────────────────────────────────
   Lightweight subviews inside an already-top-level tab/container - never
   a second nav bar, never a second backend URL. Each function below only
   toggles .hidden/.active on elements that already exist in index.html;
   none of them touch the data-loading functions those elements contain
   (loadWardrobe, getRecommendation, loadFeed, etc. are unaffected by
   which subtab is currently visible). ─────────────────────────────── */

function showDashboardSubtab(sub) {
  document.getElementById('dashboard-overview-view').classList.toggle('hidden', sub !== 'overview');
  document.getElementById('dashboard-actions-view').classList.toggle('hidden', sub !== 'actions');
  document.getElementById('dashboard-subtab-overview').classList.toggle('active', sub === 'overview');
  document.getElementById('dashboard-subtab-actions').classList.toggle('active', sub === 'actions');
}

function showAiStylistSubtab(sub) {
  document.getElementById('ai-stylist-chat-view').classList.toggle('hidden', sub !== 'chat');
  document.getElementById('ai-stylist-recommendations-view').classList.toggle('hidden', sub !== 'recommendations');
  document.getElementById('ai-subtab-chat').classList.toggle('active', sub === 'chat');
  document.getElementById('ai-subtab-recommendations').classList.toggle('active', sub === 'recommendations');
}

function showExploreSubtab(sub) {
  document.getElementById('explore-feed-view').classList.toggle('hidden', sub !== 'feed');
  document.getElementById('explore-my-posts-view').classList.toggle('hidden', sub !== 'my-posts');
  document.getElementById('explore-search-people-view').classList.toggle('hidden', sub !== 'search-people');
  document.getElementById('explore-subtab-feed').classList.toggle('active', sub === 'feed');
  document.getElementById('explore-subtab-my-posts').classList.toggle('active', sub === 'my-posts');
  document.getElementById('explore-subtab-search-people').classList.toggle('active', sub === 'search-people');
  // Search People is lazy: only fetched the first time its subtab is
  // actually opened, same convention as Explore's own first-open load
  // above and the Messages "new conversation" panel it mirrors.
  if (sub === 'search-people') initSearchPeopleIfNeeded();
}

/* ── Cross-tab quick actions (Dashboard overview) ───────────────────
   "Add an Item" and "Get a Recommendation" used to scrollIntoView a
   field on the same page; both fields now live on a different
   subtab/tab than Overview, so reaching them means switching
   tab+subtab first, then scrolling - same end result, just no longer a
   same-page scroll. */

function goToDashboardAddItem() {
  showDashboardSubtab('actions');
  document.getElementById('add-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function goToRecommendationForm() {
  // Reuses showChatTab() (does exactly `showTab('chat')` +
  // `showAiStylistSubtab('recommendations')`, unchanged) instead of
  // duplicating those two calls, so this quick-action navigation also
  // gets a real history entry - Back from here correctly returns to
  // Dashboard, same as any other section switch.
  showChatTab();
  document.getElementById('r-occasion').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* ── Init / teardown, called from app.js on login/logout ─────────── */

function initExplore() {
  // exploreLoaded must be reset *before* activateTab(), not after - if
  // the restored tab is 'explore', showExploreTab()'s own
  // if(!exploreLoaded) gate must see it as not-yet-loaded so the initial
  // feed/my-posts/outfit-picker fetches actually fire (requirement F).
  exploreLoaded = false;
  // Restores whichever top-level section the URL's fragment names -
  // boot()'s fresh-page-load/direct-open case (e.g. a bookmarked
  // /ui/#explore, or a deep link that survived a session-expired ->
  // re-login round trip), or a normal login right after an explicit
  // logout, which by now has already reset the fragment to 'dashboard'
  // (see teardownExplore() below) rather than leaving whatever section
  // was open before sign-out. 'replace' so restoring on load never adds
  // an extra Back-stop before the first section actually seen. An empty
  // or unrecognized fragment safely resolves to 'dashboard'
  // (tabFromHash()), matching the pre-existing default exactly.
  activateTab(tabFromHash(), 'replace');
  refreshUnreadCount();
  connectWebSocket();
}

function teardownExplore() {
  disconnectWebSocket();
  exploreLoaded = false;
  unreadCount = 0;
  updateBellBadge();
  document.getElementById('notification-panel').classList.add('hidden');
  notificationPanelOpen = false;
  currentThreadUserId = null;
  currentThreadUsername = null;
  revokeThreadBlobUrls();
  document.getElementById('thread-view').classList.add('hidden');
  document.getElementById('inbox-view').classList.remove('hidden');
  clearRecreateState();
  closeProfileModal();
  searchPeopleLoaded = false;
  showTab('dashboard');
  // Logout must leave the unauthenticated app - and its own address bar -
  // in a sensible canonical navigation state, not a stale authenticated
  // section fragment (e.g. #messages) left over from before sign-out.
  // Without this, a normal login right afterward in the same tab would
  // call initExplore() above, which reads that same still-present
  // fragment and would silently reopen Messages/Chat/whatever instead of
  // the expected default Dashboard - a real, if narrow, information leak
  // between whoever was signed in before and whoever logs in next on the
  // same browser tab. Uses replaceState (not pushState/syncTabHistory),
  // deliberately unconditional on currentHistoryTab's prior value -
  // logout always resets to the canonical default, it never adds a new
  // Back-stop of its own. This does NOT affect a genuine direct deep
  // link (a fresh page load at /ui/#explore, or a session-expired ->
  // re-login round trip on that same URL) - those never go through
  // teardownExplore() at all; only an explicit Sign Out does.
  history.replaceState({ tab: 'dashboard' }, '', '#dashboard');
  currentHistoryTab = 'dashboard';
}
