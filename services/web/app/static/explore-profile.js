/* ── Search People + shared public-profile view ────────────────────
   One lightweight modal (#profile-modal in index.html) reused from three
   entry points: an Explore post's "View Profile" button, a Search People
   result's "View Profile" button, and a Messages thread's "View Profile"
   button (see viewCurrentThreadProfile). It only ever shows what the
   backend already exposes publicly: {id, username} from GET /users, that
   user's own non-anonymous posts from GET /posts?author_id= (reusing the
   exact same postCard() renderer explore-posts.js already uses for the
   Feed/My Posts lists - no second post-rendering implementation), and the
   existing Message action (startThreadWith, already defined in
   explore-messages.js). No followers, presence, bio, or profile-editing -
   none of that data exists on the backend and none is added here.

   Closing the modal never navigates anywhere: it's an overlay on top of
   whichever tab was already open (Explore, or a Messages thread), so
   "return to the existing conversation" is simply "close the modal". ── */

/* ── Search People ──────────────────────────────────────────────── */

let searchPeopleLoaded = false;
let searchPeopleDebounce = null;

function initSearchPeopleIfNeeded() {
  if (searchPeopleLoaded) return;
  searchPeopleLoaded = true;
  searchPeople('');
}

document.getElementById('search-people-input').addEventListener('input', e => {
  clearTimeout(searchPeopleDebounce);
  const term = e.target.value;
  searchPeopleDebounce = setTimeout(() => searchPeople(term), 300);
});

async function searchPeople(term) {
  setLoading('search-people-status', 'Loading…');
  document.getElementById('search-people-empty').classList.add('hidden');
  try {
    const params = new URLSearchParams({ page_size: '20' });
    if (term.trim()) params.set('search', term.trim());
    // A small, deliberate duplicate of the ~5-line GET /users? fetch
    // already in explore-messages.js's searchNewThreadUsers - not hoisted
    // into a shared helper because that file's own fetch call is asserted
    // on verbatim by an existing regression test
    // (test_new_conversation_js_never_bypasses_auth_headers), and nothing
    // else about this search (its render/pagination-less result list) is
    // shared with that panel.
    const res = await fetch(`/users?${params}`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('search-people-status');
    renderSearchPeopleResults(data.items);
  } catch {
    setError('search-people-status', 'Could not load members.', () => searchPeople(term));
  }
}

function renderSearchPeopleResults(users) {
  const list = document.getElementById('search-people-results');
  const empty = document.getElementById('search-people-empty');
  if (users.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  list.innerHTML = users.map(searchPeopleRow).join('');
}

function searchPeopleRow(user) {
  return `
    <div class="inbox-row">
      <span class="inbox-username">${esc(user.username)}</span>
      <div class="flex-row-sm">
        <button type="button" class="btn btn-ghost btn-sm"
                data-user-id="${esc(user.id)}" data-username="${esc(user.username)}"
                onclick="openProfileFromButton(this)">View Profile</button>
        <button type="button" class="btn btn-ghost btn-sm"
                data-user-id="${esc(user.id)}" data-username="${esc(user.username)}"
                onclick="startThreadWithFromButton(this)">Message</button>
      </div>
    </div>`;
}

/* ── Shared public-profile modal ───────────────────────────────────
   Entry points: Explore post cards' "View Profile" (explore-posts.js),
   Search People rows above, and a Messages thread's "View Profile"
   (viewCurrentThreadProfile, below) - all funnel through
   openProfileFromButton/openUserProfile so there is exactly one profile
   render path. */

let profileUserId = null;
let profileUsername = null;
let profilePostsPage = 1;
let profilePostsHasMore = false;
let profilePostsItems = [];

function openProfileFromButton(btn) {
  openUserProfile(btn.dataset.userId, btn.dataset.username);
}

/* Reached from a Messages thread's "View Profile" button - reuses the
   currentThreadUserId/currentThreadUsername explore-messages.js already
   tracks for the open thread (see openThread), rather than re-deriving
   them some other way. */
function viewCurrentThreadProfile() {
  if (!currentThreadUserId) return;
  openUserProfile(currentThreadUserId, currentThreadUsername);
}

function openUserProfile(userId, username) {
  profileUserId = userId;
  profileUsername = username;
  document.getElementById('profile-modal-username').textContent = username;

  const messageBtn = document.getElementById('profile-modal-message-btn');
  messageBtn.onclick = () => {
    closeProfileModal();
    startThreadWith(userId, username);
  };

  document.getElementById('profile-modal').classList.remove('hidden');
  profilePostsPage = 1;
  profilePostsItems = [];
  fetchProfilePostsPage(false);
}

function closeProfileModal() {
  document.getElementById('profile-modal').classList.add('hidden');
}

async function fetchProfilePostsPage(append) {
  setLoading('profile-posts-status', 'Loading…');
  try {
    const res = await fetch(
      `/posts?author_id=${encodeURIComponent(profileUserId)}&page=${profilePostsPage}&page_size=10`,
      { headers: authHeaders() }
    );
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('profile-posts-status');
    profilePostsHasMore = data.has_more;
    profilePostsItems = append ? profilePostsItems.concat(data.items) : data.items;
    renderProfilePosts();
  } catch {
    setError('profile-posts-status', "Could not load this member's posts.", () => fetchProfilePostsPage(append));
  }
}

function loadMoreProfilePosts() {
  profilePostsPage += 1;
  fetchProfilePostsPage(true);
}

function renderProfilePosts() {
  const list = document.getElementById('profile-posts-list');
  const empty = document.getElementById('profile-posts-empty');
  if (profilePostsItems.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    // Reuses the exact same post-card renderer as the Feed/My Posts lists
    // (explore-posts.js) - never a second post-rendering implementation.
    list.innerHTML = profilePostsItems.map(post => postCard(post, false)).join('');
  }
  document.getElementById('profile-posts-load-more').classList.toggle('hidden', !profilePostsHasMore);
}
