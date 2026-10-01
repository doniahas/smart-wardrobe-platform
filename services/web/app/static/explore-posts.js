/* ── Explore: posts, comments, reactions ─────────────────────────── */

let feedPage = 1;
let feedHasMore = false;
let feedItems = [];

let myPostsPage = 1;
let myPostsHasMore = false;
let myPostsItems = [];

/* postId -> { page, hasMore, items } once that post's comments have
   been expanded at least once. */
const commentsStateByPost = {};

/* ── Shared outfit (Recreate with My Wardrobe) ─────────────────────
   The post-creation "share one of my outfits" picker, plus rendering a
   post's shared_outfit snapshot and its Recreate button/results. The
   snapshot only ever carries the five categorical fields per component
   (see backend models.py) - no wardrobe/outfit ids are ever present in
   this data to begin with, so there's nothing to redact here. */

async function loadOutfitsForPicker() {
  const select = document.getElementById('post-shared-outfit');
  try {
    const res = await fetch('/outfits', { headers: authHeaders() });
    if (!res.ok) return;
    const outfits = await res.json();
    const options = outfits.map(o =>
      `<option value="${esc(o.id)}">${esc(o.name || 'Untitled outfit')}</option>`
    ).join('');
    select.innerHTML = `<option value="">No shared outfit</option>${options}`;
  } catch {
    /* Leave the default "No shared outfit" option in place. */
  }
}

function sharedOutfitComponentChip(component) {
  const emoji = EMOJI[component.clothing_type] || '👔';
  return `<span class="outfit-item-chip"><span class="chip-emoji">${emoji}</span>${esc(component.color)} ${esc(component.clothing_type)}</span>`;
}

function sharedOutfitBlock(post) {
  if (!post.shared_outfit) return '';
  const outfit = post.shared_outfit;
  const chips = outfit.components.map(sharedOutfitComponentChip).join('');
  return `
    <div class="comments-block">
      <div class="comment-author">Shared Outfit${outfit.outfit_name ? ': ' + esc(outfit.outfit_name) : ''}</div>
      <div class="outfit-items">${chips}</div>
      <button type="button" class="btn btn-ghost btn-sm" onclick="toggleRecreate('${esc(post.id)}')">Recreate with My Wardrobe</button>
      <div id="recreate-${esc(post.id)}" class="comments-block hidden"></div>
    </div>`;
}

async function toggleRecreate(postId) {
  const block = document.getElementById(`recreate-${postId}`);
  const isHidden = block.classList.contains('hidden');
  block.classList.toggle('hidden');
  if (isHidden && !block.dataset.loaded) {
    block.dataset.loaded = '1';
    await runRecreate(postId);
  }
}

async function runRecreate(postId) {
  const block = document.getElementById(`recreate-${postId}`);
  block.innerHTML = '<p class="hint">Loading…</p>';
  try {
    const res = await fetch(`/posts/${postId}/recreate`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    renderRecreateResults(postId, data);
  } catch {
    block.innerHTML = `<p id="recreate-err-${postId}"></p>`;
    setError(`recreate-err-${postId}`, 'Could not load recreation results.', () => runRecreate(postId));
  }
}

function recreateMatchRow(match) {
  const comp = match.component;
  const emoji = EMOJI[comp.clothing_type] || '👔';
  if (!match.matched_item) {
    return `
      <div class="comment-item">
        <div class="comment-author">${emoji} ${esc(comp.clothing_type)} &middot; not in your wardrobe</div>
        <div class="comment-body">${esc(match.match_explanation)}</div>
      </div>`;
  }
  const pct = Math.round(match.match_score * 100);
  return `
    <div class="comment-item">
      <div class="comment-author">${emoji} ${esc(match.matched_item.name)} &middot; ${pct}% match</div>
      <div class="comment-body">${esc(match.match_explanation)}</div>
    </div>`;
}

/* ── Outfit Builder handoff ────────────────────────────────────────
   Reads RecreateMatchResult rows from the structured recreate path
   (GET /posts/{id}/recreate) and hands the matched wardrobe item ids to
   the Builder - only ever matched_item.id, never anything else. It lives
   inside renderRecreateResults below, so the caller has no Builder logic
   of its own.

   Grounding: the only ids that ever reach the Builder come from
   matched_item, i.e. real wardrobe documents the backend matcher
   verified belong to the requesting user. A component with
   matched_item === null contributes nothing, and no AI-detected
   name/type/color is ever read here - the detected component is
   display data only, never an identifier. */

/* postId -> the real wardrobe item ids from that post's last
   recreation. Keyed state rather than ids embedded in an onclick
   attribute, matching commentsStateByPost's pattern above. */
const recreateItemIdsByPost = {};

/* postId -> the clothing types Recreate could not match in this user's
   wardrobe, passed to the Builder purely as context for shop
   suggestions (see outfit-builder.js). */
const recreateMissingTypesByPost = {};

function matchedWardrobeItemIds(matches) {
  return matches
    .filter(m => m.matched_item)
    .map(m => m.matched_item.id);
}

/* The unmatched rows' clothing types, deduplicated, in the order the
   matcher returned them. Read only from component.clothing_type on rows
   where matched_item is null - a structured enum value that came back
   from the backend, never from AI free text, a product name, or any
   other source. */
function unmatchedRecreateTypes(matches) {
  const seen = new Set();
  const types = [];
  matches.forEach(m => {
    if (m.matched_item) return;
    const type = m.component && m.component.clothing_type;
    if (type && !seen.has(type)) {
      seen.add(type);
      types.push(type);
    }
  });
  return types;
}

/* Called by teardownExplore() on logout: these are real wardrobe item
   ids, cleared for the same reason logout() clears wardrobeItemsById. */
function clearRecreateState() {
  Object.keys(recreateItemIdsByPost).forEach(k => delete recreateItemIdsByPost[k]);
  Object.keys(recreateMissingTypesByPost).forEach(k => delete recreateMissingTypesByPost[k]);
}

function openRecreatedOutfitInBuilder(postId) {
  const itemIds = recreateItemIdsByPost[postId] || [];
  if (!itemIds.length) return;
  /* The existing Builder, opened in its normal create mode: it stays
     responsible for customizing, naming, and saving. Nothing is saved
     here - this only prefills the selection. No occasion/season/style
     is passed, since neither Recreate response carries them.
     missingTypes is advisory context only; it never affects the
     selection, which stays exactly the matched owned items. */
  openOutfitBuilder({
    itemIds,
    missingTypes: recreateMissingTypesByPost[postId] || [],
  });
}

function renderRecreateResults(postId, data) {
  const block = document.getElementById(`recreate-${postId}`);
  const matches = data.matches;
  const total = matches.length;
  const matchedItemIds = matchedWardrobeItemIds(matches);
  const matchedCount = matchedItemIds.length;
  const avgScore = matches.reduce((sum, m) => sum + (m.match_score || 0), 0) / total;
  const missingTypes = matches.filter(m => !m.matched_item).map(m => m.component.clothing_type);
  const missingHtml = missingTypes.length
    ? `<p class="hint">Missing from your wardrobe: ${missingTypes.map(esc).join(', ')}</p>`
    : '';

  recreateItemIdsByPost[postId] = matchedItemIds;
  recreateMissingTypesByPost[postId] = unmatchedRecreateTypes(matches);

  /* No real item matched - there is nothing grounded to hand over, so
     no Builder button at all (rather than one that opens it empty). The
     match rows and any missing messaging above still explain why. */
  const builderHtml = matchedCount
    ? `<button type="button" class="btn btn-primary btn-sm"
               onclick="openRecreatedOutfitInBuilder('${esc(postId)}')">Customize this outfit</button>`
    : '';

  block.innerHTML = `
    <div class="totals-banner">
      <div class="totals-stat">
        <div class="totals-value">${matchedCount}/${total}</div>
        <div class="totals-label">Items Matched</div>
      </div>
      <div class="totals-stat">
        <div class="totals-value">${Math.round(avgScore * 100)}%</div>
        <div class="totals-label">Overall Match</div>
      </div>
    </div>
    ${missingHtml}
    ${matches.map(recreateMatchRow).join('')}
    ${builderHtml}`;
}

/* ── Post card ──────────────────────────────────────────────────── */

/* Explore (post/comment) attachments are public, so - unlike DM
   attachments in explore-messages.js - a plain <img>/<video src> is
   enough; no authenticated fetch/blob-URL indirection is needed here. */
function attachmentHtml(a) {
  return a.media_type === 'video'
    ? `<video src="${esc(a.url)}" controls></video>`
    : `<img src="${esc(a.url)}" alt="attachment">`;
}

function postCard(post, isMine) {
  const authorHtml = post.is_anonymous
    ? `<span class="post-author anonymous">Anonymous</span>`
    : (isMine
        ? `<span class="post-author">${esc(post.author_username)}</span>`
        : `<span class="post-author">${esc(post.author_username)}</span>
           <button type="button" class="btn btn-ghost btn-sm"
                   data-user-id="${esc(post.author_id)}" data-username="${esc(post.author_username)}"
                   onclick="openProfileFromButton(this)">View Profile</button>
           <button type="button" class="btn btn-ghost btn-sm"
                   data-user-id="${esc(post.author_id)}" data-username="${esc(post.author_username)}"
                   onclick="startThreadWithFromButton(this)">Message</button>`);

  const attachmentsHtml = (post.attachments && post.attachments.length)
    ? `<div class="post-attachments">${post.attachments.map(attachmentHtml).join('')}</div>`
    : '';

  const deleteBtn = isMine
    ? `<button type="button" class="btn btn-danger btn-sm" onclick="deletePost('${esc(post.id)}')">Delete</button>`
    : '';

  return `
    <div class="post-card" id="post-${esc(post.id)}">
      <div class="post-card-header">
        <div>${authorHtml}</div>
        <span class="post-date">${formatDate(post.created_at)}</span>
      </div>
      <div class="post-title">${esc(post.title)}</div>
      <div class="post-body">${esc(post.body)}</div>
      ${attachmentsHtml}
      ${sharedOutfitBlock(post)}
      <div class="post-actions">
        <button type="button" class="reaction-btn like ${post.my_reaction === 1 ? 'active' : ''}"
                onclick="reactToPost('${esc(post.id)}', 1)">&#128077; <span id="post-${esc(post.id)}-likes">${post.likes_count}</span></button>
        <button type="button" class="reaction-btn dislike ${post.my_reaction === -1 ? 'active' : ''}"
                onclick="reactToPost('${esc(post.id)}', -1)">&#128078; <span id="post-${esc(post.id)}-dislikes">${post.dislikes_count}</span></button>
        <button type="button" class="btn btn-ghost btn-sm" onclick="toggleComments('${esc(post.id)}')">
          Comments (<span id="post-${esc(post.id)}-comments-count">${post.comments_count}</span>)
        </button>
        ${deleteBtn}
      </div>
      <div id="comments-${esc(post.id)}" class="comments-block hidden"></div>
    </div>`;
}

function startThreadWithFromButton(btn) {
  startThreadWith(btn.dataset.userId, btn.dataset.username);
}

/* ── Create post ────────────────────────────────────────────────── */

document.getElementById('post-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('post-msg');

  const images = document.getElementById('post-images').files;
  if (images.length > 4) {
    showMsg('post-msg', 'You can attach at most 4 images.', 'err');
    return;
  }

  const formData = new FormData();
  formData.append('title', document.getElementById('post-title').value.trim());
  formData.append('body', document.getElementById('post-body').value.trim());
  formData.append('is_anonymous', document.getElementById('post-anonymous').checked ? 'true' : 'false');
  const sharedOutfitId = document.getElementById('post-shared-outfit').value;
  if (sharedOutfitId) formData.append('shared_outfit_id', sharedOutfitId);
  Array.from(images).forEach(f => formData.append('images', f));

  const submitBtn = e.target.querySelector('button[type="submit"]');
  await withButtonPending(submitBtn, 'Posting…', async () => {
    try {
      const res = await fetch('/posts', { method: 'POST', headers: authHeadersMultipart(), body: formData });
      if (res.status === 201) {
        showMsg('post-msg', 'Post published.', 'ok');
        document.getElementById('post-form').reset();
        loadFeed();
      } else {
        const d = await res.json();
        showMsg('post-msg', formatApiError(d.detail, 'Failed to create post.'), 'err');
      }
    } catch {
      showMsg('post-msg', 'Network error.', 'err');
    }
  });
});

/* ── Feed ───────────────────────────────────────────────────────── */

async function loadFeed() {
  feedPage = 1;
  feedItems = [];
  await fetchFeedPage(false);
}

function loadMoreFeed() {
  feedPage += 1;
  fetchFeedPage(true);
}

async function fetchFeedPage(append) {
  setLoading('feed-status', 'Loading…');
  try {
    const res = await fetch(`/posts?page=${feedPage}&page_size=10`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('feed-status');
    feedHasMore = data.has_more;
    feedItems = append ? feedItems.concat(data.items) : data.items;
    renderFeed();
  } catch {
    setError('feed-status', 'Could not load the feed.', () => fetchFeedPage(append));
  }
}

function renderFeed() {
  const list = document.getElementById('feed-list');
  const empty = document.getElementById('feed-empty');
  if (feedItems.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    list.innerHTML = feedItems.map(post => postCard(post, false)).join('');
  }
  document.getElementById('feed-load-more').classList.toggle('hidden', !feedHasMore);
}

/* ── My Posts ───────────────────────────────────────────────────── */

async function loadMyPosts() {
  myPostsPage = 1;
  myPostsItems = [];
  await fetchMyPostsPage(false);
}

function loadMoreMyPosts() {
  myPostsPage += 1;
  fetchMyPostsPage(true);
}

async function fetchMyPostsPage(append) {
  setLoading('my-posts-status', 'Loading…');
  try {
    const res = await fetch(`/posts/mine?page=${myPostsPage}&page_size=10`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus('my-posts-status');
    myPostsHasMore = data.has_more;
    myPostsItems = append ? myPostsItems.concat(data.items) : data.items;
    document.getElementById('my-posts-totals').classList.remove('hidden');
    document.getElementById('my-posts-likes').textContent = data.total_likes_received;
    document.getElementById('my-posts-dislikes').textContent = data.total_dislikes_received;
    renderMyPosts();
  } catch {
    setError('my-posts-status', 'Could not load your posts.', () => fetchMyPostsPage(append));
  }
}

function renderMyPosts() {
  const list = document.getElementById('my-posts-list');
  const empty = document.getElementById('my-posts-empty');
  if (myPostsItems.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    list.innerHTML = myPostsItems.map(post => postCard(post, true)).join('');
  }
  document.getElementById('my-posts-load-more').classList.toggle('hidden', !myPostsHasMore);
}

async function deletePost(postId) {
  if (!confirm('Delete this post?')) return;
  try {
    const res = await fetch(`/posts/${postId}`, { method: 'DELETE', headers: authHeaders() });
    if (res.status === 204) {
      loadMyPosts();
      loadFeed();
    } else {
      alert('Failed to delete post.');
    }
  } catch {
    alert('Network error.');
  }
}

/* ── Post reactions ─────────────────────────────────────────────── */

async function reactToPost(postId, value) {
  try {
    const res = await fetch(`/posts/${postId}/reactions`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ value }),
    });
    if (!res.ok) return;
    const d = await res.json();
    patchReactionUI(`post-${postId}`, d);
  } catch { /* silent */ }
}

function patchReactionUI(prefix, reaction) {
  const container = document.getElementById(prefix);
  if (!container) return;
  const likeBtn = container.querySelector('.reaction-btn.like');
  const dislikeBtn = container.querySelector('.reaction-btn.dislike');
  const likesEl = document.getElementById(`${prefix}-likes`);
  const dislikesEl = document.getElementById(`${prefix}-dislikes`);
  if (likesEl) likesEl.textContent = reaction.likes_count;
  if (dislikesEl) dislikesEl.textContent = reaction.dislikes_count;
  if (likeBtn) likeBtn.classList.toggle('active', reaction.my_reaction === 1);
  if (dislikeBtn) dislikeBtn.classList.toggle('active', reaction.my_reaction === -1);
}

/* ── Comments ───────────────────────────────────────────────────── */

function ensureCommentsSkeleton(postId) {
  const block = document.getElementById(`comments-${postId}`);
  if (block.dataset.ready) return;
  block.dataset.ready = '1';
  block.innerHTML = `
    <p id="comments-status-${postId}" class="hint"></p>
    <div id="comments-empty-${postId}" class="empty-state hidden">
      <p class="empty-title">No comments yet</p>
      <p class="empty-sub">Be the first to comment.</p>
    </div>
    <div id="comments-list-${postId}"></div>
    <div id="comments-load-more-${postId}" class="load-more-row hidden">
      <button type="button" class="btn btn-ghost btn-sm" onclick="loadMoreComments('${postId}')">Load more</button>
    </div>
    <form class="comment-form" onsubmit="return submitComment(event, '${postId}')">
      <textarea placeholder="Write a comment..." maxlength="1000" required></textarea>
      <input type="file" accept="image/jpeg,image/jpg,image/png,image/webp,video/mp4" multiple>
      <button type="submit" class="btn btn-primary btn-sm">Comment</button>
      <p class="msg" id="comment-msg-${postId}"></p>
    </form>`;
}

async function toggleComments(postId) {
  const block = document.getElementById(`comments-${postId}`);
  const isHidden = block.classList.contains('hidden');
  if (isHidden) {
    block.classList.remove('hidden');
    ensureCommentsSkeleton(postId);
    if (!commentsStateByPost[postId]) {
      commentsStateByPost[postId] = { page: 1, hasMore: false, items: [] };
      await fetchCommentsPage(postId, false);
    } else {
      // loadFeed() rebuilds every post's DOM from scratch (a brand
      // new, empty comments-${postId} element), but commentsStateByPost
      // survives that rebuild since it's plain JS state, not DOM-bound.
      // Without this, cached items would be silently stranded - present
      // in state, never rendered into the newly-rebuilt (empty) list -
      // until something else (e.g. posting a new comment) forced a
      // render. Re-render the cached state now instead of re-fetching it.
      renderComments(postId);
    }
  } else {
    block.classList.add('hidden');
  }
}

function loadMoreComments(postId) {
  commentsStateByPost[postId].page += 1;
  fetchCommentsPage(postId, true);
}

async function fetchCommentsPage(postId, append) {
  const state = commentsStateByPost[postId];
  setLoading(`comments-status-${postId}`, 'Loading comments…');
  try {
    const res = await fetch(`/posts/${postId}/comments?page=${state.page}&page_size=10`, { headers: authHeaders() });
    if (!res.ok) throw new Error();
    const data = await res.json();
    clearStatus(`comments-status-${postId}`);
    state.hasMore = data.has_more;
    state.items = append ? state.items.concat(data.items) : data.items;
    renderComments(postId);
  } catch {
    setError(`comments-status-${postId}`, 'Could not load comments.', () => fetchCommentsPage(postId, append));
  }
}

function renderComments(postId) {
  const state = commentsStateByPost[postId];
  const list = document.getElementById(`comments-list-${postId}`);
  const empty = document.getElementById(`comments-empty-${postId}`);
  if (state.items.length === 0) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    list.innerHTML = state.items.map(commentCard).join('');
  }
  document.getElementById(`comments-load-more-${postId}`).classList.toggle('hidden', !state.hasMore);
}

function commentCard(comment) {
  const attachmentsHtml = (comment.attachments && comment.attachments.length)
    ? `<div class="comment-attachments">${comment.attachments.map(attachmentHtml).join('')}</div>`
    : '';
  return `
    <div class="comment-item" id="comment-${esc(comment.id)}">
      <div class="comment-author">${esc(comment.author_username)} &middot; ${formatDate(comment.created_at)}</div>
      <div class="comment-body">${esc(comment.body)}</div>
      ${attachmentsHtml}
      <div class="post-actions">
        <button type="button" class="reaction-btn like ${comment.my_reaction === 1 ? 'active' : ''}"
                onclick="reactToComment('${esc(comment.id)}', 1)">&#128077; <span id="comment-${esc(comment.id)}-likes">${comment.likes_count}</span></button>
        <button type="button" class="reaction-btn dislike ${comment.my_reaction === -1 ? 'active' : ''}"
                onclick="reactToComment('${esc(comment.id)}', -1)">&#128078; <span id="comment-${esc(comment.id)}-dislikes">${comment.dislikes_count}</span></button>
      </div>
    </div>`;
}

async function submitComment(event, postId) {
  event.preventDefault();
  const form = event.target;
  const textarea = form.querySelector('textarea');
  const fileInput = form.querySelector('input[type=file]');
  clearMsg(`comment-msg-${postId}`);

  if (fileInput.files.length > 4) {
    showMsg(`comment-msg-${postId}`, 'You can attach at most 4 images.', 'err');
    return false;
  }

  const formData = new FormData();
  formData.append('body', textarea.value.trim());
  Array.from(fileInput.files).forEach(f => formData.append('images', f));

  try {
    const res = await fetch(`/posts/${postId}/comments`, {
      method: 'POST',
      headers: authHeadersMultipart(),
      body: formData,
    });
    if (res.status === 201) {
      form.reset();
      commentsStateByPost[postId].page = 1;
      await fetchCommentsPage(postId, false);
      const countEl = document.getElementById(`post-${postId}-comments-count`);
      if (countEl) countEl.textContent = Number(countEl.textContent) + 1;
    } else {
      const d = await res.json();
      showMsg(`comment-msg-${postId}`, formatApiError(d.detail, 'Failed to add comment.'), 'err');
    }
  } catch {
    showMsg(`comment-msg-${postId}`, 'Network error.', 'err');
  }
  return false;
}

/* ── Comment reactions ──────────────────────────────────────────── */

async function reactToComment(commentId, value) {
  try {
    const res = await fetch(`/comments/${commentId}/reactions`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ value }),
    });
    if (!res.ok) return;
    const d = await res.json();
    patchReactionUI(`comment-${commentId}`, d);
  } catch { /* silent */ }
}
