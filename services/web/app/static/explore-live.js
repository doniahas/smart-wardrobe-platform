/* ── Explore: WebSocket live delivery ──────────────────────────────
   Connects right after login (not only while the Explore/Messages tab
   is open), authenticates via the documented first-frame protocol,
   and reconnects on a simple fixed delay if the connection drops
   while the user is still logged in. MongoDB remains the source of
   truth throughout - this only nudges the UI to refresh already-
   persisted data; nothing here is ever the only copy of anything. ──*/

let ws = null;
let wsReconnectTimer = null;
const WS_RECONNECT_DELAY_MS = 4000;

function connectWebSocket() {
  if (!getToken() || ws) return;

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

  ws.onopen = () => {
    ws.send(JSON.stringify({ token: getToken() }));
  };

  ws.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.event === 'notification') {
      handleLiveNotification(data.notification);
    }
  };

  ws.onclose = () => {
    ws = null;
    if (getToken()) {
      wsReconnectTimer = setTimeout(connectWebSocket, WS_RECONNECT_DELAY_MS);
    }
  };

  ws.onerror = () => {
    if (ws) ws.close();
  };
}

function disconnectWebSocket() {
  clearTimeout(wsReconnectTimer);
  wsReconnectTimer = null;
  if (ws) {
    ws.onclose = null; // don't reconnect on a deliberate close
    ws.close();
    ws = null;
  }
}

function handleLiveNotification(notif) {
  unreadCount += 1;
  updateBellBadge();

  if (notificationPanelOpen) {
    notificationsItems.unshift(notif);
    renderNotifications();
  }

  const messagesTabOpen = !document.getElementById('messages-app').classList.contains('hidden');
  if (messagesTabOpen) loadInbox();

  if (notif.type === 'message' && currentThreadUserId === notif.actor_id) {
    threadPage = 1;
    fetchThreadPage(false);
  }
}
