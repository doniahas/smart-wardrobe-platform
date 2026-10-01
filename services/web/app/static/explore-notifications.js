/* ── Explore: notification bell + panel ────────────────────────── */

let notificationsPage = 1;
let notificationsHasMore = false;
let notificationsItems = [];
let unreadCount = 0;
let notificationPanelOpen = false;

async function refreshUnreadCount() {
  try {
    const res = await fetch('/notifications/unread-count', { headers: authHeaders() });
    if (!res.ok) return;
    const d = await res.json();
    unreadCount = d.count;
    updateBellBadge();
  } catch { /* silent */ }
}

function updateBellBadge() {
  const badge = document.getElementById('bell-badge');
  if (unreadCount > 0) {
    badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

function toggleNotificationPanel() {
  const panel = document.getElementById('notification-panel');
  const willOpen = panel.classList.contains('hidden');
  panel.classList.toggle('hidden');
  notificationPanelOpen = willOpen;
  if (willOpen) {
    notificationsPage = 1;
    notificationsItems = [];
    fetchNotificationsPage(false);
  }
}

async function fetchNotificationsPage(append) {
  setLoading('notifications-status', 'Loading…');
  try {
    const res = await fetch(`/notifications?page=${notificationsPage}&page_size=20`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('notifications-status');
    notificationsHasMore = data.has_more;
    notificationsItems = append ? notificationsItems.concat(data.items) : data.items;
    renderNotifications();
  } catch {
    setError('notifications-status', 'Could not load notifications.', () => fetchNotificationsPage(append));
  }
}

function loadMoreNotifications() {
  notificationsPage += 1;
  fetchNotificationsPage(true);
}

function renderNotifications() {
  const list = document.getElementById('notifications-list');
  const empty = document.getElementById('notifications-empty');
  if (notificationsItems.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    list.innerHTML = notificationsItems.map(notificationRow).join('');
  }
  document.getElementById('notifications-load-more').classList.toggle('hidden', !notificationsHasMore);
}

function notificationText(n) {
  switch (n.type) {
    case 'message': return `${esc(n.actor_username)} sent you a message`;
    case 'like_post': return `${esc(n.actor_username)} liked your post`;
    case 'dislike_post': return `${esc(n.actor_username)} disliked your post`;
    case 'like_comment': return `${esc(n.actor_username)} liked your comment`;
    case 'dislike_comment': return `${esc(n.actor_username)} disliked your comment`;
    default: return 'New notification';
  }
}

/* Only the server-generated id is ever interpolated into the onclick
   attribute - actor_username can contain characters (e.g. a quote)
   that would be unsafe to embed directly into an inline JS string, so
   the rest of the notification is looked up from notificationsItems
   by id at click time instead. */
function notificationRow(n) {
  return `
    <div class="notification-item ${n.read ? '' : 'unread'}" onclick="handleNotificationClick('${esc(n.id)}')">
      <span class="notification-text">${notificationText(n)}</span>
      <span class="notification-time">${formatDate(n.created_at)}</span>
    </div>`;
}

function handleNotificationClick(id) {
  const n = notificationsItems.find(x => x.id === id);
  if (!n) return;
  markNotificationRead(id);
  if (n.type === 'message') {
    toggleNotificationPanel();
    showMessagesTab();
    openThread(n.actor_id, n.actor_username);
  }
}

async function markNotificationRead(id) {
  try {
    const res = await fetch(`/notifications/${id}/read`, { method: 'POST', headers: authHeaders() });
    if (res.ok) {
      const item = notificationsItems.find(n => n.id === id);
      if (item && !item.read) {
        item.read = true;
        unreadCount = Math.max(0, unreadCount - 1);
        updateBellBadge();
        renderNotifications();
      }
    }
  } catch { /* silent */ }
}

async function markAllNotificationsRead() {
  try {
    const res = await fetch('/notifications/mark-all-read', { method: 'POST', headers: authHeaders() });
    if (res.ok) {
      notificationsItems.forEach(n => { n.read = true; });
      unreadCount = 0;
      updateBellBadge();
      renderNotifications();
    }
  } catch { /* silent */ }
}
