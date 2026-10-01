/* ── Explore: DM inbox, thread, private image loading ─────────────
   The inbox is always populated from GET /messages/conversations
   alone - it reflects every conversation the user participates in,
   regardless of whether it started via the feed's "Message" button,
   was received unprompted, or anything else. That button is only a
   shortcut to open/create a thread from the feed. ─────────────────*/

let inboxPage = 1;
let inboxHasMore = false;
let inboxItems = [];

let currentThreadUserId = null;
let currentThreadUsername = null;
let threadPage = 1;
let threadHasMore = false;
let threadItems = [];
let threadBlobUrls = [];

/* ── Inbox ──────────────────────────────────────────────────────── */

async function loadInbox() {
  inboxPage = 1;
  inboxItems = [];
  await fetchInboxPage(false);
}

function loadMoreInbox() {
  inboxPage += 1;
  fetchInboxPage(true);
}

async function fetchInboxPage(append) {
  setLoading('inbox-status', 'Loading…');
  try {
    const res = await fetch(`/messages/conversations?page=${inboxPage}&page_size=20`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('inbox-status');
    inboxHasMore = data.has_more;
    inboxItems = append ? inboxItems.concat(data.items) : data.items;
    renderInbox();
  } catch {
    setError('inbox-status', 'Could not load your conversations.', () => fetchInboxPage(append));
  }
}

function renderInbox() {
  const list = document.getElementById('inbox-list');
  const empty = document.getElementById('inbox-empty');
  if (inboxItems.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    list.innerHTML = inboxItems.map(inboxRow).join('');
  }
  document.getElementById('inbox-load-more').classList.toggle('hidden', !inboxHasMore);
}

function inboxRow(conv) {
  return `
    <div class="inbox-row" data-user-id="${esc(conv.other_user_id)}" data-username="${esc(conv.other_username)}"
         onclick="openThreadFromRow(this)">
      <span class="inbox-username">${esc(conv.other_username)}</span>
      <span class="inbox-time">${formatDate(conv.last_message_at)}</span>
    </div>`;
}

function openThreadFromRow(el) {
  openThread(el.dataset.userId, el.dataset.username);
}

function startThreadWith(userId, username) {
  showMessagesTab();
  openThread(userId, username);
}

/* ── Thread ─────────────────────────────────────────────────────── */

function openThread(userId, username) {
  currentThreadUserId = userId;
  currentThreadUsername = username;
  document.getElementById('thread-with').textContent = 'Conversation with ' + username;
  document.getElementById('thread-view').classList.remove('hidden');
  document.getElementById('inbox-view').classList.add('hidden');
  threadPage = 1;
  threadItems = [];
  revokeThreadBlobUrls();
  fetchThreadPage(false);
}

function closeThread() {
  currentThreadUserId = null;
  currentThreadUsername = null;
  revokeThreadBlobUrls();
  document.getElementById('thread-view').classList.add('hidden');
  document.getElementById('inbox-view').classList.remove('hidden');
  loadInbox();
}

function revokeThreadBlobUrls() {
  threadBlobUrls.forEach(u => URL.revokeObjectURL(u));
  threadBlobUrls = [];
}

async function fetchThreadPage(append) {
  setLoading('thread-status', 'Loading…');
  try {
    const res = await fetch(`/messages/${currentThreadUserId}?page=${threadPage}&page_size=20`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('thread-status');
    threadHasMore = data.has_more;
    // The API returns newest-first; reverse each page so it reads
    // top-to-bottom, then prepend older pages above what's shown.
    const chronological = data.items.slice().reverse();
    threadItems = append ? chronological.concat(threadItems) : chronological;
    renderThread(!append);
  } catch {
    setError('thread-status', 'Could not load messages.', () => fetchThreadPage(append));
  }
}

function loadMoreThread() {
  threadPage += 1;
  fetchThreadPage(true);
}

function renderThread(scrollToBottom) {
  revokeThreadBlobUrls();
  const container = document.getElementById('thread-messages');
  const empty = document.getElementById('thread-empty');
  if (threadItems.length === 0) {
    container.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    container.innerHTML = threadItems.map(dmBubble).join('');
    threadItems.forEach(msg => {
      (msg.attachments || []).forEach((att, i) => {
        const mediaEl = document.getElementById(`dm-media-${msg.id}-${i}`);
        if (mediaEl) loadDmMedia(att.url, mediaEl);
      });
    });
  }
  document.getElementById('thread-load-more').classList.toggle('hidden', !threadHasMore);
  if (scrollToBottom) container.scrollTop = container.scrollHeight;
}

function dmBubble(msg) {
  // A thread only ever has 2 participants (me and currentThreadUserId),
  // so anything not from the other user must be mine.
  const mine = msg.sender_id !== currentThreadUserId;
  // A placeholder <video controls> for a video attachment, exactly
  // like the pre-existing <img> placeholder for an image one - both are
  // left without a src here and filled in by loadDmMedia below, since DM
  // media can never be reached via a plain src= URL (see that function).
  const attachmentsHtml = (msg.attachments || []).map((a, i) =>
    a.media_type === 'video'
      ? `<video id="dm-media-${esc(msg.id)}-${i}" controls></video>`
      : `<img id="dm-media-${esc(msg.id)}-${i}" alt="attachment">`
  ).join('');
  return `
    <div class="dm-bubble ${mine ? 'mine' : 'theirs'}">
      <div>${esc(msg.body)}</div>
      ${attachmentsHtml ? `<div class="dm-bubble-attachments">${attachmentsHtml}</div>` : ''}
      <div class="dm-bubble-time">${formatDate(msg.created_at)}</div>
    </div>`;
}

/* Private DM media (images and MP4 videos) require the Authorization
   header, which a plain <img>/<video> src cannot send - fetched manually
   and shown via a revocable blob URL. Works unchanged for both element
   kinds: assigning .src is all either one needs. */
async function loadDmMedia(url, mediaEl) {
  try {
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) return;
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    threadBlobUrls.push(objectUrl);
    mediaEl.src = objectUrl;
  } catch { /* leave the broken placeholder */ }
}

/* ── New conversation: user discovery ──────────────────────────────
   The inbox above only ever shows conversations that already exist, and
   the Explore feed's "Message" button only ever reaches a post's real,
   non-anonymous author - neither can reach a user who hasn't posted, or
   who has posted only anonymously. This panel is the fix: an
   authenticated GET /users?search= lookup, independent of any post, then
   the exact same startThreadWith(userId, username) the Explore button
   already uses - no separate/duplicate DM-opening logic. ─────────────*/

let newThreadPanelOpen = false;
let newThreadSearchDebounce = null;

function toggleNewThreadPanel() {
  newThreadPanelOpen = !newThreadPanelOpen;
  document.getElementById('new-thread-panel').classList.toggle('hidden', !newThreadPanelOpen);
  document.getElementById('new-thread-toggle').textContent =
    newThreadPanelOpen ? 'Close' : 'Start a new conversation';
  if (newThreadPanelOpen) {
    document.getElementById('new-thread-search').value = '';
    searchNewThreadUsers('');
  }
}

document.getElementById('new-thread-search').addEventListener('input', e => {
  clearTimeout(newThreadSearchDebounce);
  const term = e.target.value;
  // Small debounce so every keystroke doesn't fire its own request - this
  // is a normal UX courtesy, not a security control (GET /users is
  // already bounded/paginated and covered by the app's default rate
  // limit regardless of how often it's called).
  newThreadSearchDebounce = setTimeout(() => searchNewThreadUsers(term), 300);
});

async function searchNewThreadUsers(term) {
  setLoading('new-thread-status', 'Loading…');
  document.getElementById('new-thread-empty').classList.add('hidden');
  try {
    const params = new URLSearchParams({ page_size: '20' });
    if (term.trim()) params.set('search', term.trim());
    const res = await fetch(`/users?${params}`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('new-thread-status');
    renderNewThreadResults(data.items);
  } catch {
    setError('new-thread-status', 'Could not load members.', () => searchNewThreadUsers(term));
  }
}

function renderNewThreadResults(users) {
  const list = document.getElementById('new-thread-results');
  const empty = document.getElementById('new-thread-empty');
  if (users.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  list.innerHTML = users.map(newThreadRow).join('');
}

function newThreadRow(user) {
  return `
    <div class="inbox-row">
      <span class="inbox-username">${esc(user.username)}</span>
      <button type="button" class="btn btn-ghost btn-sm"
              data-user-id="${esc(user.id)}" data-username="${esc(user.username)}"
              onclick="startNewThreadFromButton(this)">Message</button>
    </div>`;
}

function startNewThreadFromButton(btn) {
  newThreadPanelOpen = false;
  document.getElementById('new-thread-panel').classList.add('hidden');
  document.getElementById('new-thread-toggle').textContent = 'Start a new conversation';
  startThreadWith(btn.dataset.userId, btn.dataset.username);
}

/* ── Send message ───────────────────────────────────────────────── */

document.getElementById('thread-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('thread-msg');
  if (!currentThreadUserId) return;

  const images = document.getElementById('thread-images').files;
  if (images.length > 4) {
    showMsg('thread-msg', 'You can attach at most 4 images.', 'err');
    return;
  }

  const formData = new FormData();
  formData.append('body', document.getElementById('thread-body').value.trim());
  Array.from(images).forEach(f => formData.append('images', f));

  const submitBtn = e.target.querySelector('button[type="submit"]');
  await withButtonPending(submitBtn, 'Sending…', async () => {
    try {
      const res = await fetch(`/messages/${currentThreadUserId}`, {
        method: 'POST',
        headers: authHeadersMultipart(),
        body: formData,
      });
      if (res.status === 201) {
        document.getElementById('thread-form').reset();
        threadPage = 1;
        await fetchThreadPage(false);
      } else {
        const d = await res.json();
        showMsg('thread-msg', formatApiError(d.detail, 'Failed to send message.'), 'err');
      }
    } catch {
      showMsg('thread-msg', 'Network error.', 'err');
    }
  });
});
