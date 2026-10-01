/* ── AI Chat ───────────────────────────────────────────────────────
   User-facing chat UI for the app's local LLM. Deliberately uses ONLY
   the persistent, queued job architecture (`POST /chat/jobs` +
   `GET /chat/jobs/{id}`, see services/chat_queue.py + chat_worker.py) -
   never the synchronous `POST /chat` endpoint, which remains in the API
   for backward compatibility/testing but is not the normal product path.
   Relies on esc()/authHeaders()/clearMsg()/showMsg() from app.js - safe
   because, like outfit-builder.js, nothing here calls them until a user
   interaction fires (after every script has already loaded). ─────── */

const CHAT_POLL_INTERVAL_MS = 1500;
// Client-side safety cap only, well above CHAT_JOB_STALE_TIMEOUT_SECONDS x
// CHAT_JOB_MAX_ATTEMPTS worst case - stops a single request from polling
// forever if something is badly wrong server-side, without ever needing to
// know the server's exact retry/timeout configuration.
const CHAT_POLL_MAX_MS = 6 * 60 * 1000;

const CHAT_ERROR_MESSAGES = {
  llm_timeout: 'The AI took too long to respond. Please try again.',
  llm_unavailable: 'The AI service is currently unavailable. Please try again shortly.',
  stale_abandoned: 'This request could not be completed after several attempts. Please try again.',
};

function chatErrorText(errorReason) {
  return CHAT_ERROR_MESSAGES[errorReason] || 'Something went wrong generating a response. Please try again.';
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

let chatSubmitting = false;

function setChatFormEnabled(enabled) {
  document.getElementById('chat-input').disabled = !enabled;
  const btn = document.getElementById('chat-send-btn');
  btn.disabled = !enabled;
  btn.textContent = enabled ? 'Send' : 'Sending…';
}

function chatBubbleHtml(role, id, text) {
  const cls = role === 'user' ? 'mine' : 'theirs';
  return `<div class="dm-bubble ${cls}" id="${esc(id)}">${esc(text)}</div>`;
}

function appendChatHtml(html) {
  document.getElementById('chat-empty').classList.add('hidden');
  const log = document.getElementById('chat-log');
  log.insertAdjacentHTML('beforeend', html);
  log.scrollTop = log.scrollHeight;
}

/* `state` is 'pending' (queued/processing - subtle pulse, see style.css),
   'error' (failed job / network / auth problem), or omitted entirely for
   a normal completed reply - never more than one of the two modifier
   classes is applied at once. */
function setChatBubbleText(bubbleId, text, state) {
  const el = document.getElementById(bubbleId);
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('chat-bubble-error', state === 'error');
  el.classList.toggle('chat-bubble-pending', state === 'pending');
  document.getElementById('chat-log').scrollTop = document.getElementById('chat-log').scrollHeight;
}

/* Poll a submitted job until it reaches a terminal state (or the client-side
   safety cap is hit). Never crashes the page on a failed/errored job -
   every branch below ends by re-enabling the form so the user can keep
   chatting. */
async function pollChatJob(jobId, bubbleId) {
  const startedAt = Date.now();
  while (true) {
    if (Date.now() - startedAt > CHAT_POLL_MAX_MS) {
      setChatBubbleText(bubbleId, 'This is taking unusually long - please try again in a moment.', 'error');
      break;
    }

    await sleep(CHAT_POLL_INTERVAL_MS);

    let res;
    try {
      res = await fetch(`/chat/jobs/${jobId}`, { headers: authHeaders() });
    } catch {
      // Transient network hiccup - keep polling rather than giving up.
      continue;
    }

    if (res.status === 401) {
      setChatBubbleText(bubbleId, 'Your session expired - please sign in again.', 'error');
      break;
    }
    if (!res.ok) {
      setChatBubbleText(bubbleId, 'Could not check on your message - please try again.', 'error');
      break;
    }

    const job = await res.json();
    if (job.status === 'completed') {
      setChatBubbleText(bubbleId, job.result || '(empty response)');
      break;
    }
    if (job.status === 'failed') {
      setChatBubbleText(bubbleId, chatErrorText(job.error_reason), 'error');
      break;
    }
    // Still "pending" or "processing" - keep the waiting state visible and
    // distinguish the two so a busy queue doesn't look identical to "the
    // AI is actively thinking".
    setChatBubbleText(bubbleId, job.status === 'processing' ? 'Thinking…' : 'Waiting in queue…', 'pending');
  }

  chatSubmitting = false;
  setChatFormEnabled(true);
  document.getElementById('chat-input').focus();
}

document.getElementById('chat-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('chat-msg');

  // Prevents accidental duplicate submissions while a job is in flight.
  if (chatSubmitting) return;

  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message) return;

  chatSubmitting = true;
  setChatFormEnabled(false);

  appendChatHtml(chatBubbleHtml('user', `chat-msg-${Date.now()}`, message));
  input.value = '';

  const bubbleId = `chat-pending-${Date.now()}`;
  appendChatHtml(chatBubbleHtml('assistant', bubbleId, 'Sending…'));
  document.getElementById(bubbleId).classList.add('chat-bubble-pending');

  try {
    const res = await fetch('/chat/jobs', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ message }),
    });
    if (res.status !== 202) {
      const d = await res.json().catch(() => ({}));
      setChatBubbleText(bubbleId, formatApiError(d.detail, 'Could not send your message. Please try again.'), 'error');
      chatSubmitting = false;
      setChatFormEnabled(true);
      return;
    }
    const job = await res.json();
    setChatBubbleText(bubbleId, job.status === 'processing' ? 'Thinking…' : 'Waiting in queue…', 'pending');
    await pollChatJob(job.job_id, bubbleId);
  } catch {
    setChatBubbleText(bubbleId, 'Network error - please try again.', 'error');
    chatSubmitting = false;
    setChatFormEnabled(true);
  }
});

/* Called from app.js's logout() - the chat log is per-session only (there
   is no "list my past jobs" endpoint), so it's simply cleared, matching
   how the rest of the app resets on sign-out. */
function teardownChat() {
  chatSubmitting = false;
  document.getElementById('chat-log').innerHTML =
    '<p id="chat-empty" class="hint">Ask your AI stylist anything - e.g. "What goes well with a navy blazer?"</p>';
  document.getElementById('chat-form').reset();
  setChatFormEnabled(true);
  clearMsg('chat-msg');
}
