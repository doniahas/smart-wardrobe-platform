/* ── Wardrobe door intro (purely decorative — does not touch auth,
   wardrobe, recommendation, or rating logic in any way) ───────────
   Fully deterministic by design: the door slide and overlay fade are
   pure CSS @keyframes animations with fixed delays/durations (see
   style.css) that start automatically on render. This script does
   NOT listen for transitionend or animationend anywhere — nothing
   here depends on a CSS event actually firing.
     - guard clause if the markup is missing
     - sessionStorage / matchMedia calls are defensive
     - a single timer removes the overlay node ~2.5s after load, just
       after the CSS animation has already finished fading it out
     - Skip always calls overlay.remove() immediately — no fade, no
       class, no dependency on anything else
     - even if this whole script fails to run, the CSS animation's own
       forwards fill-mode independently drives the overlay to
       opacity:0 / visibility:hidden / pointer-events:none by ~2.4s,
       so the site can never be permanently blocked               ── */
(function initWardrobeIntro() {
  const overlay = document.getElementById('wardrobe-intro');
  if (!overlay) return;

  let removed = false;
  function removeOverlay() {
    if (removed || !overlay.isConnected) return;
    removed = true;
    overlay.remove();
  }

  const skipBtn = document.getElementById('wardrobe-intro-skip');
  if (skipBtn) skipBtn.addEventListener('click', removeOverlay);

  let alreadyShown = false;
  try { alreadyShown = sessionStorage.getItem('sw_intro_shown') === '1'; }
  catch (e) { /* storage unavailable — treat as not shown yet */ }

  let reducedMotion = false;
  try { reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches; }
  catch (e) { /* matchMedia unavailable — proceed with the animation */ }

  if (alreadyShown || reducedMotion) {
    removeOverlay();
    return;
  }

  try { sessionStorage.setItem('sw_intro_shown', '1'); }
  catch (e) { /* ignore — worst case the intro plays again next load */ }

  // The CSS animation fades the overlay out on its own by ~2.4s; this
  // just detaches the (by then invisible) node from the DOM afterward.
  setTimeout(removeOverlay, 2500);
})();

/* ── Token helpers ──────────────────────────────────────────────── */

function getToken()    { return localStorage.getItem('sw_token'); }
function saveToken(t)  { localStorage.setItem('sw_token', t); }
function clearToken()  { localStorage.removeItem('sw_token'); }

/* Cross-tab auth sync. The browser's native `storage` event fires
   automatically in every *other* same-origin tab whenever localStorage
   changes (never in the tab that made the change itself) - so this is
   the only listener needed to catch a login/logout/account switch that
   happened in another tab. A full reload is the simplest safe response:
   it re-runs boot() from scratch against whatever sw_token now holds,
   guaranteeing every piece of UI state (username, visible tabs, loaded
   data) is correctly re-derived, rather than trying to patch each piece
   individually. Does not touch DM/WebSocket logic - a reload naturally
   re-establishes the WebSocket connection the same way a real page load
   already does. */
window.addEventListener('storage', event => {
  if (event.key === 'sw_token') {
    location.reload();
  }
});

function authHeaders() {
  return {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${getToken()}`,
  };
}

/* For multipart/form-data requests — the browser sets its own boundary,
   so we must not set Content-Type manually. */
function authHeadersMultipart() {
  return { 'Authorization': `Bearer ${getToken()}` };
}

/* ── Message helpers ────────────────────────────────────────────── */

function showMsg(id, text, type /* 'ok' | 'err' */) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = `msg ${type}`;
}

function clearMsg(id) {
  const el = document.getElementById(id);
  el.textContent = '';
  el.className = 'msg';
}

/**
 * Turns a FastAPI error-response `detail` into a safe, human-readable
 * string for display - never the raw object/array. Handles exactly the
 * shapes FastAPI can actually produce:
 *  - a plain string (every hand-written HTTPException in this app) -
 *    displayed unchanged;
 *  - an array of Pydantic validation-error objects (FastAPI's own
 *    automatic 422 response) - only each entry's string `msg` is shown,
 *    joined clearly and deduplicated, never `loc`/`input`/the raw object;
 *  - a single object with a string `msg` or `message` field - that text
 *    only, nothing else on the object;
 *  - anything else (an object with no usable text, malformed/absent
 *    detail, an empty array, a non-JSON body) - the caller's own existing
 *    fallback string.
 * Never returns anything derived from `input`, `loc`, or any field not
 * explicitly named above - request bodies and other response fields are
 * never touched or surfaced.
 */
function formatApiError(detail, fallback) {
  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }
  if (Array.isArray(detail)) {
    const seen = new Set();
    const msgs = [];
    for (const entry of detail) {
      if (entry && typeof entry === 'object' && typeof entry.msg === 'string' && entry.msg.trim()) {
        if (!seen.has(entry.msg)) {
          seen.add(entry.msg);
          msgs.push(entry.msg);
        }
      }
    }
    if (msgs.length) return msgs.join('; ');
  }
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const text = (typeof detail.msg === 'string' && detail.msg.trim())
      ? detail.msg
      : (typeof detail.message === 'string' && detail.message.trim() ? detail.message : null);
    if (text) return text;
  }
  return fallback;
}

/* ── Button pending-state helper (UI polish) ────────────────────────
   Wraps an in-flight async action so its triggering button is disabled
   and shows a pending label for the duration - gives visual feedback on
   slower requests and prevents an accidental double-submit as a side
   effect. Always restores the button's original label/disabled state
   afterward, even if the wrapped function throws. Declared here (app.js,
   loaded last) but called from explore-posts.js/explore-messages.js too -
   safe because, like esc()/authHeaders()/showMsg(), nothing calls it
   until a user interaction fires, after every script has already
   loaded (same reasoning chat.js's own header comment already relies
   on for its app.js helpers). `btn` may be null - a no-op guard, never
   a hard requirement on every call site having one. */
async function withButtonPending(btn, pendingLabel, fn) {
  if (!btn) return fn();
  const originalLabel = btn.textContent;
  const originalDisabled = btn.disabled;
  btn.disabled = true;
  btn.textContent = pendingLabel;
  try {
    return await fn();
  } finally {
    btn.disabled = originalDisabled;
    btn.textContent = originalLabel;
  }
}

/* ── UI state ───────────────────────────────────────────────────── */

function showApp(username) {
  document.getElementById('hero-section').classList.add('hidden');
  document.getElementById('auth-section').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  document.getElementById('user-bar').classList.remove('hidden');
  document.getElementById('username-display').textContent = username;
  // Header reorg: the account dropdown's own (optional) username header
  // reuses this same already-known username - no new backend call.
  document.getElementById('account-dropdown-username').textContent = username;
  // UI polish pass: dashboard greeting reuses this same already-known
  // username - no new backend call.
  const greetingEl = document.getElementById('dashboard-greeting');
  if (greetingEl) greetingEl.textContent = `Welcome back, ${username}`;
  window.scrollTo({ top: 0 });
}

function showAuth() {
  document.getElementById('hero-section').classList.remove('hidden');
  document.getElementById('auth-section').classList.remove('hidden');
  // Every top-level authenticated container, hidden explicitly and
  // unconditionally - this function is the authoritative "logged out"
  // state setter, regardless of which tab/subtab was active or what ran
  // before it (see logout()'s own note on why it's called last there).
  document.getElementById('app').classList.add('hidden');
  document.getElementById('explore-app').classList.add('hidden');
  document.getElementById('messages-app').classList.add('hidden');
  document.getElementById('chat-app').classList.add('hidden');
  document.getElementById('user-bar').classList.add('hidden');
  document.getElementById('rec-result').classList.add('hidden');
  closeAccountMenu();
  clearRecMissing();
}

/* ── Account menu (header reorg) ─────────────────────────────────────
   Sign Out moved from an always-visible header button into a small
   dropdown under the username - same toggle-panel pattern already used
   for the color picker/multi-select above (one open panel at a time,
   closed by an outside click), reusing the existing logout() unchanged. */

function toggleAccountMenu() {
  document.getElementById('account-dropdown').classList.toggle('hidden');
}

function closeAccountMenu() {
  document.getElementById('account-dropdown').classList.add('hidden');
}

document.addEventListener('click', e => {
  const dropdown = document.getElementById('account-dropdown');
  if (dropdown.classList.contains('hidden')) return;
  if (!e.target.closest('.account-menu')) closeAccountMenu();
});

/* ── Escape HTML ────────────────────────────────────────────────── */

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                        .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── Clothing type emoji map ────────────────────────────────────── */

const EMOJI = {
  top: '👕', bottom: '👖', dress: '👗',
  outerwear: '🧥', shoes: '👟', accessory: '👒',
};

/* ── Item card HTML ─────────────────────────────────────────────── */

function itemCard(item, withDelete) {
  const emoji = EMOJI[item.clothing_type] || '👔';
  const thumb = item.image_url
    ? `<div class="item-thumb"><img src="${esc(item.image_url)}" alt="${esc(item.name)}"
         onerror="this.outerHTML='<span style=\\'font-size:2.8rem\\'>${emoji}</span>'"></div>`
    : `<div class="item-thumb">${emoji}</div>`;

  const deleteBtn = withDelete
    ? `<div class="item-actions">
         <button class="btn btn-ghost btn-sm" onclick="openEditModal('${esc(item.id)}')">Edit</button>
         <button class="btn btn-danger" onclick="deleteItem('${esc(item.id)}')">Remove</button>
       </div>`
    : '';

  return `
    <div class="item-card" id="card-${esc(item.id)}">
      ${thumb}
      <div class="item-body">
        <div class="item-name">${esc(item.name)}</div>
        <div class="item-color">
          <span class="color-dot" style="background:${esc(item.color_hex || colorHex(item.color))}"></span>
          ${esc(item.color)}
        </div>
        <div class="badges">
          <span class="badge">${esc(item.clothing_type)}</span>
          <span class="badge season">${esc(item.season)}</span>
          ${(item.style || []).map(s => `<span class="badge style">${esc(s)}</span>`).join('')}
          ${(item.occasion || []).map(o => `<span class="badge occasion">${esc(o)}</span>`).join('')}
        </div>
      </div>
      ${deleteBtn}
    </div>`;
}

/* ── Boot: check saved token ────────────────────────────────────── */

async function boot() {
  if (!getToken()) { showAuth(); return; }
  try {
    const res = await fetch('/auth/me', { headers: authHeaders() });
    if (res.ok) {
      const user = await res.json();
      showApp(user.username);
      loadWardrobe();
      loadSavedOutfits();
      loadMemory();
      initExplore();
    } else {
      clearToken();
      showAuth();
    }
  } catch {
    showAuth();
  }
}

/* ── Register ───────────────────────────────────────────────────── */

document.getElementById('register-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('register-msg');
  const body = {
    username: document.getElementById('reg-username').value.trim(),
    email:    document.getElementById('reg-email').value.trim(),
    password: document.getElementById('reg-password').value,
  };
  try {
    const res = await fetch('/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (res.status === 201) {
      showMsg('register-msg', 'Account created. You can now sign in.', 'ok');
      document.getElementById('register-form').reset();
    } else {
      const d = await res.json();
      showMsg('register-msg', formatApiError(d.detail, 'Registration failed.'), 'err');
    }
  } catch {
    showMsg('register-msg', 'Network error — is the server running?', 'err');
  }
});

/* ── Login ──────────────────────────────────────────────────────── */

document.getElementById('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('login-msg');
  const body = {
    username: document.getElementById('login-username').value.trim(),
    password: document.getElementById('login-password').value,
  };
  try {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const d = await res.json();
      saveToken(d.access_token);
      document.getElementById('login-form').reset();
      const me = await fetch('/auth/me', { headers: authHeaders() });
      const user = await me.json();
      showApp(user.username);
      loadWardrobe();
      loadSavedOutfits();
      loadMemory();
      initExplore();
    } else {
      const d = await res.json();
      showMsg('login-msg', formatApiError(d.detail, 'Login failed.'), 'err');
    }
  } catch {
    showMsg('login-msg', 'Network error — is the server running?', 'err');
  }
});

/* ── Logout ─────────────────────────────────────────────────────── */

function logout() {
  clearToken();
  // teardownExplore()/teardownChat() run before showAuth(), not after -
  // teardownExplore() ends with showTab('dashboard') (its normal reset-to-
  // default-tab behavior), which un-hides #app as a side effect. Calling
  // showAuth() afterward instead of before makes it the authoritative,
  // last-applied "logged out" state - its own hides can never be undone by
  // teardown logic that runs after it.
  teardownExplore();
  teardownChat();
  document.getElementById('wardrobe-grid').innerHTML = '';
  document.getElementById('wardrobe-empty').classList.add('hidden');
  document.getElementById('saved-outfits-grid').innerHTML = '';
  document.getElementById('saved-outfits-empty').classList.add('hidden');
  document.getElementById('stat-wardrobe-count').textContent = '–';
  document.getElementById('stat-outfits-count').textContent = '–';
  lastRecItemIds = [];
  feedbackByOutfit = {};
  wardrobeItemsById = {};
  savedOutfitsById = {};
  closeOutfitBuilder();
  closeEditModal();
  document.getElementById('memory-form').reset();
  clearMsg('memory-msg');
  showAuth();
  document.getElementById('auth-section').scrollIntoView({ behavior: 'smooth' });
}

/* ── Load wardrobe ──────────────────────────────────────────────── */

let wardrobeItemsById = {};

async function loadWardrobe() {
  try {
    const res = await fetch('/wardrobe', { headers: authHeaders() });
    if (!res.ok) return;
    const items = await res.json();
    renderWardrobe(items);
  } catch { /* silent on load */ }
}

function renderWardrobe(items) {
  const grid  = document.getElementById('wardrobe-grid');
  const empty = document.getElementById('wardrobe-empty');
  wardrobeItemsById = {};
  items.forEach(item => { wardrobeItemsById[item.id] = item; });
  document.getElementById('stat-wardrobe-count').textContent = items.length;
  if (items.length === 0) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    grid.innerHTML = items.map(item => itemCard(item, true)).join('');
  }
}

/* ── Add wardrobe item ──────────────────────────────────────────── */

function previewImageFile(fileInput, imgEl) {
  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    imgEl.classList.add('hidden');
    imgEl.src = '';
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    imgEl.src = reader.result;
    imgEl.classList.remove('hidden');
  };
  reader.readAsDataURL(file);
}

document.getElementById('i-image').addEventListener('change', e => {
  previewImageFile(e.target, document.getElementById('i-image-preview'));
  document.getElementById('i-analyze-btn').disabled = !e.target.files[0];
  clearMsg('i-analyze-msg');
  document.getElementById('i-ai-note').classList.add('hidden');
});

/* Prefill the add-item form from the AI's detected-garment suggestion.
   Only fields the model actually returned are written; a null leaves
   whatever the user already had. */
function applyGarmentSuggestion(s) {
  if (s.name)          document.getElementById('i-name').value = s.name;
  if (s.color)         setColorPickerValue('i', s.color, s.color_hex);
  if (s.clothing_type) document.getElementById('i-type').value = s.clothing_type;
  if (s.season)        document.getElementById('i-season').value = s.season;
  if (s.style)         setMultiSelectValues('i-style', s.style);
  if (s.occasion)      setMultiSelectValues('i-occasion', s.occasion);
}

/* ── AI-assisted photo analysis (Analyze Item) ───────────────────────
   Suggestions only: prefills the form fields below but never submits or
   saves anything itself - "Add to Wardrobe" remains the only persistence
   action, and every prefilled field stays fully editable. If this fails
   for any reason, manual entry is unaffected. */

document.getElementById('i-analyze-btn').addEventListener('click', async () => {
  const btn = document.getElementById('i-analyze-btn');
  const file = document.getElementById('i-image').files[0];
  if (!file) return;

  btn.disabled = true;
  btn.textContent = 'Analyzing…';
  clearMsg('i-analyze-msg');
  document.getElementById('i-ai-note').classList.add('hidden');

  const formData = new FormData();
  formData.append('image', file);

  try {
    const res = await fetch('/wardrobe/analyze-image', {
      method: 'POST',
      headers: authHeadersMultipart(),
      body: formData,
    });
    if (res.ok) {
      applyGarmentSuggestion(await res.json());
      showMsg('i-analyze-msg', 'AI suggestions applied — review every field before saving.', 'ok');
      document.getElementById('i-ai-note').classList.remove('hidden');
    } else {
      showMsg('i-analyze-msg', 'AI suggestions unavailable — you can still fill this in manually.', 'err');
    }
  } catch {
    showMsg('i-analyze-msg', 'AI suggestions unavailable — you can still fill this in manually.', 'err');
  } finally {
    btn.disabled = !document.getElementById('i-image').files[0];
    btn.textContent = 'Analyze photo';
  }
});

/* ── Wardrobe Color picker (curated shade grid) ──────────────────────
   Single source of truth for the frontend: the add form, edit form, and
   the wardrobe-card swatch dot all read from this one data structure -
   none of them hardcodes its own color list.
   The backend keeps its own copy of the 14 category names and each
   category's representative shade (wardrobe_ai.py's WARDROBE_COLOR_
   PALETTE / _COLOR_PALETTE_HEX) to steer AI suggestions onto the same
   values; there's no shared build step between the Python backend and
   this static JS frontend, so the two stay in sync by convention, not by
   import.

   Each category maps to a handful of curated {hex} shades - a fixed,
   predefined list, never an arbitrary/free-form color. The first shade in
   each list is that category's representative/default shade (used as the
   AI suggestion's starting swatch and as the legacy-item fallback), kept
   identical to Pass 1's original 14 default hexes so nothing that already
   depended on them shifts. */
const WARDROBE_COLOR_SHADES = {
  black:  ['#111111', '#000000', '#2b2b2b'],
  white:  ['#ffffff', '#f5f5f0', '#eae6df'],
  gray:   ['#888888', '#d3d3d3', '#a9a9a9', '#5a5a5a', '#333333'],
  navy:   ['#1f2d50', '#14213d', '#2a3f66'],
  blue:   ['#2b6cb0', '#4a90d9', '#1c4e80', '#6fa8dc', '#17344d'],
  green:  ['#2f7d32', '#4caf50', '#1b5e20', '#7cb342', '#75a478'],
  olive:  ['#6b8e23', '#808000', '#556b2f', '#9caf88'],
  beige:  ['#d9c9a3', '#e8dcc0', '#c8b89a', '#b8a888', '#ede0c8'],
  brown:  ['#6b4423', '#8b5a2b', '#4a2f1a', '#a0714f', '#5c4033'],
  red:    ['#c0392b', '#e74c3c', '#922b21', '#7b241c', '#d98880'],
  pink:   ['#e07a9e', '#f4a7c1', '#d63384', '#c2708a', '#f8c8dc'],
  purple: ['#7d4bb0', '#9b59b6', '#4a235a', '#b39ddb', '#5e3370'],
  yellow: ['#d4b106', '#f1c40f', '#b8860b', '#e8d174'],
  orange: ['#d2691e', '#e67e22', '#a0522d', '#f0a868'],
};

/* {name, hex} per category, hex = that category's default/representative
   shade - the exact same shape Pass 1's WARDROBE_COLOR_PALETTE had, so
   every existing consumer (colorHex, the wardrobe-card dot, etc.) keeps
   working unchanged. */
const WARDROBE_COLOR_PALETTE = Object.keys(WARDROBE_COLOR_SHADES).map(name => ({
  name, hex: WARDROBE_COLOR_SHADES[name][0],
}));

function colorCategoryLabel(name) {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/* A category's default swatch hex, case-insensitively. Falls back to
   using the raw value as a CSS color - the same thing the wardrobe-card
   dot did before this palette existed - so a legacy saved color that
   isn't one of the 14 names but still happens to be a valid CSS color
   word still renders correctly, and finally to a neutral placeholder so a
   legacy value that's neither never renders as a broken/transparent dot.
   Callers that have an item's own color_hex should prefer that directly
   over calling this - this is only ever the category's generic default. */
function colorHex(value) {
  const match = WARDROBE_COLOR_PALETTE.find(c => c.name === String(value || '').trim().toLowerCase());
  if (match) return match.hex;
  return value ? value : '#cccccc';
}

function updateColorDot(dotId, value) {
  const dot = document.getElementById(dotId);
  if (dot) dot.style.background = colorHex(value);
}

/* The swatch grid for one color picker instance, grouped by category.
   `selectedCategory`/`selectedHex` mark the current value's swatch as
   already selected at render time (used when a picker is populated with
   an existing item's color on open). */
function buildColorSwatchGridHtml(prefix, selectedCategory, selectedHex) {
  return WARDROBE_COLOR_PALETTE.map(({ name }) => {
    const swatches = WARDROBE_COLOR_SHADES[name].map(hex => {
      const isSelected = name === selectedCategory && hex === selectedHex;
      return `
      <button type="button" class="color-picker-swatch${isSelected ? ' selected' : ''}" data-category="${name}" data-hex="${hex}"
              style="background:${hex}" title="${esc(colorCategoryLabel(name))}"
              onclick="selectColorSwatch('${prefix}','${name}','${hex}')"></button>`;
    }).join('');
    return `
      <div class="color-picker-group">
        <div class="color-picker-group-label">${esc(colorCategoryLabel(name))}</div>
        <div class="color-picker-swatches">${swatches}</div>
      </div>`;
  }).join('');
}

function closeColorPicker(prefix) {
  const panel = document.getElementById(`${prefix}-color-panel`);
  if (panel) panel.classList.add('hidden');
}

function toggleColorPicker(prefix) {
  document.querySelectorAll('.color-picker-panel').forEach(panel => {
    if (panel.id !== `${prefix}-color-panel`) panel.classList.add('hidden');
  });
  const panel = document.getElementById(`${prefix}-color-panel`);
  if (panel) panel.classList.toggle('hidden');
}

/* Closes any open picker panel when clicking outside it - one delegated
   listener covers both the add-form and edit-form picker instances. */
document.addEventListener('click', e => {
  document.querySelectorAll('.color-picker-panel:not(.hidden)').forEach(panel => {
    const picker = panel.closest('.color-picker');
    if (picker && !picker.contains(e.target)) panel.classList.add('hidden');
  });
});

function highlightColorSwatch(prefix, category, hex) {
  const panel = document.getElementById(`${prefix}-color-panel`);
  if (!panel) return;
  panel.querySelectorAll('.color-picker-swatch.selected').forEach(el => el.classList.remove('selected'));
  if (!category || !hex) return;
  const match = panel.querySelector(`.color-picker-swatch[data-category="${category}"][data-hex="${hex}"]`);
  if (match) match.classList.add('selected');
}

/* Sets one picker instance's value from a swatch pick - both the
   semantic category (color) and the exact shade (color_hex) always move
   together, since every swatch is a predefined {category, hex} pair. */
function selectColorSwatch(prefix, category, hex) {
  document.getElementById(`${prefix}-color`).value = category;
  document.getElementById(`${prefix}-color-hex`).value = hex;
  document.getElementById(`${prefix}-color-dot`).style.background = hex;
  document.getElementById(`${prefix}-color-label`).textContent = colorCategoryLabel(category);
  highlightColorSwatch(prefix, category, hex);
  closeColorPicker(prefix);
}

/* Sets a picker's value from a stored item (color, color_hex) - used for
   the AI suggestion and for opening the edit form. If color_hex is
   missing (a Pass 1/legacy item that only has a semantic category) the
   category's own default shade is shown instead, purely as a starting
   swatch - never treated as if it were the item's real recorded shade.
   If color itself isn't one of the 14 categories (a pre-palette free-text
   value), it's preserved as-is and labeled "(current)", exactly like Pass
   1's select-based fallback - no swatch is force-selected, and the value
   is only replaced if the user deliberately picks one. */
function setColorPickerValue(prefix, colorValue, colorHexValue) {
  const lowered = String(colorValue || '').trim().toLowerCase();
  const isKnownCategory = WARDROBE_COLOR_PALETTE.some(c => c.name === lowered);
  const category = isKnownCategory ? lowered : '';
  const hex = colorHexValue || (isKnownCategory ? colorHex(lowered) : '');

  document.getElementById(`${prefix}-color`).value = isKnownCategory ? lowered : (colorValue || '');
  document.getElementById(`${prefix}-color-hex`).value = hex;
  document.getElementById(`${prefix}-color-dot`).style.background = hex || colorHex(colorValue);
  document.getElementById(`${prefix}-color-label`).textContent = isKnownCategory
    ? colorCategoryLabel(lowered)
    : (colorValue ? `${colorValue} (current)` : 'Select a color…');

  highlightColorSwatch(prefix, category, hex);
}

function resetColorPicker(prefix) {
  document.getElementById(`${prefix}-color`).value = '';
  document.getElementById(`${prefix}-color-hex`).value = '';
  document.getElementById(`${prefix}-color-dot`).style.background = '';
  document.getElementById(`${prefix}-color-label`).textContent = 'Select a color…';
  highlightColorSwatch(prefix, null, null);
  closeColorPicker(prefix);
}

document.getElementById('i-color-panel').innerHTML = buildColorSwatchGridHtml('i');
document.getElementById('e-color-panel').innerHTML = buildColorSwatchGridHtml('e');

/* Allowed values for the Style/Occasion multi-select (see below) - the
   only fields that still read from an options list at this point. */
const WARDROBE_FIELD_OPTIONS = {
  style: ['casual', 'formal', 'sport', 'streetwear', 'elegant', 'minimal'],
  occasion: ['casual', 'work', 'formal', 'sport', 'university', 'travel'],
};

function optionLabel(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/* ── Multi-select (Style / Occasion) ─────────────────────────────────
   Style and occasion are lists now - a garment may fit more than one of
   each (see models.py). Single reusable component (trigger button +
   floating checklist panel + removable chips), shared by the add form
   and edit form. State lives in multiSelectState, keyed by the same
   "i-style"/"e-occasion"-style prefix used throughout this file. */
const multiSelectState = {};
const multiSelectOptions = {};

function initMultiSelect(prefix, options, initial) {
  multiSelectOptions[prefix] = options;
  multiSelectState[prefix] = Array.isArray(initial) ? [...initial] : [];
  renderMultiSelect(prefix);
}

function renderMultiSelect(prefix) {
  const options = multiSelectOptions[prefix] || [];
  const selected = multiSelectState[prefix] || [];

  const chipsEl = document.getElementById(`${prefix}-chips`);
  if (chipsEl) {
    chipsEl.innerHTML = selected.length
      ? selected.map(value => `
          <span class="chip">${esc(optionLabel(value))}<button type="button" class="chip-remove"
                onclick="event.stopPropagation();toggleMultiSelectValue('${prefix}','${value}')"
                aria-label="Remove ${esc(optionLabel(value))}">&times;</button></span>`).join('')
      : `<span class="multi-select-placeholder">Select ${prefix.includes('occasion') ? 'occasions' : 'styles'}&hellip;</span>`;
  }

  const panel = document.getElementById(`${prefix}-panel`);
  if (panel) {
    panel.innerHTML = options.map(value => `
      <label class="multi-select-option">
        <input type="checkbox" ${selected.includes(value) ? 'checked' : ''}
               onchange="toggleMultiSelectValue('${prefix}','${value}')">
        ${esc(optionLabel(value))}
      </label>`).join('');
  }
}

function toggleMultiSelectValue(prefix, value) {
  const selected = multiSelectState[prefix] || (multiSelectState[prefix] = []);
  const idx = selected.indexOf(value);
  if (idx === -1) selected.push(value); else selected.splice(idx, 1);
  renderMultiSelect(prefix);
}

function toggleMultiSelectPanel(prefix) {
  document.querySelectorAll('.multi-select-panel').forEach(p => {
    if (p.id !== `${prefix}-panel`) p.classList.add('hidden');
  });
  const panel = document.getElementById(`${prefix}-panel`);
  if (panel) panel.classList.toggle('hidden');
}

document.addEventListener('click', e => {
  document.querySelectorAll('.multi-select-panel:not(.hidden)').forEach(panel => {
    const wrap = panel.closest('.multi-select');
    if (wrap && !wrap.contains(e.target)) panel.classList.add('hidden');
  });
});

function getMultiSelectValues(prefix) {
  return multiSelectState[prefix] || [];
}

/* Sets a picker's selection from a stored/suggested value - accepts a
   list (the normal case now), a legacy scalar string, or null/undefined.
   Anything not one of this field's known options is silently dropped
   (defensive - every value reaching here should already be validated
   server-side). */
function setMultiSelectValues(prefix, values) {
  const options = multiSelectOptions[prefix] || [];
  const raw = Array.isArray(values) ? values : (values ? [values] : []);
  const valid = raw.map(v => String(v).toLowerCase()).filter(v => options.includes(v));
  multiSelectState[prefix] = [...new Set(valid)];
  renderMultiSelect(prefix);
}

initMultiSelect('i-style', WARDROBE_FIELD_OPTIONS.style, []);
initMultiSelect('i-occasion', WARDROBE_FIELD_OPTIONS.occasion, []);
initMultiSelect('e-style', WARDROBE_FIELD_OPTIONS.style, []);
initMultiSelect('e-occasion', WARDROBE_FIELD_OPTIONS.occasion, []);

document.getElementById('add-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('add-msg');

  if (!document.getElementById('i-color').value) {
    showMsg('add-msg', 'Please choose a color.', 'err');
    return;
  }
  if (!getMultiSelectValues('i-style').length) {
    showMsg('add-msg', 'Please choose at least one style.', 'err');
    return;
  }
  if (!getMultiSelectValues('i-occasion').length) {
    showMsg('add-msg', 'Please choose at least one occasion.', 'err');
    return;
  }

  const formData = new FormData();
  formData.append('name',          document.getElementById('i-name').value.trim());
  formData.append('color',         document.getElementById('i-color').value.trim());
  formData.append('color_hex',     document.getElementById('i-color-hex').value.trim());
  formData.append('clothing_type', document.getElementById('i-type').value);
  formData.append('season',        document.getElementById('i-season').value);
  getMultiSelectValues('i-style').forEach(v => formData.append('style', v));
  getMultiSelectValues('i-occasion').forEach(v => formData.append('occasion', v));
  const imageFile = document.getElementById('i-image').files[0];
  if (imageFile) formData.append('image', imageFile);

  const submitBtn = e.target.querySelector('button[type="submit"]');
  await withButtonPending(submitBtn, 'Adding…', async () => {
    try {
      const res = await fetch('/wardrobe', {
        method: 'POST',
        headers: authHeadersMultipart(),
        body: formData,
      });
      if (res.status === 201) {
        showMsg('add-msg', 'Item added to your wardrobe.', 'ok');
        document.getElementById('add-form').reset();
        resetColorPicker('i');
        setMultiSelectValues('i-style', []);
        setMultiSelectValues('i-occasion', []);
        document.getElementById('i-image-preview').classList.add('hidden');
        document.getElementById('i-analyze-btn').disabled = true;
        clearMsg('i-analyze-msg');
        document.getElementById('i-ai-note').classList.add('hidden');
        loadWardrobe();
      } else {
        const d = await res.json();
        showMsg('add-msg', formatApiError(d.detail, 'Failed to add item.'), 'err');
      }
    } catch {
      showMsg('add-msg', 'Network error.', 'err');
    }
  });
});

/* ── Delete wardrobe item ───────────────────────────────────────── */

async function deleteItem(id) {
  if (!confirm('Remove this item from your wardrobe?')) return;
  try {
    const res = await fetch(`/wardrobe/${id}`, {
      method: 'DELETE',
      headers: authHeaders(),
    });
    if (res.status === 204) {
      loadWardrobe();
    } else {
      alert('Failed to remove item.');
    }
  } catch {
    alert('Network error.');
  }
}

/* ── Edit wardrobe item ──────────────────────────────────────────── */

document.getElementById('e-image').addEventListener('change', e => {
  previewImageFile(e.target, document.getElementById('e-image-preview'));
  // Choosing a replacement photo supersedes a pending removal - keep the
  // checkbox in sync so the UI never shows both "remove" checked and a
  // new-file preview at once (the submit handler would also resolve this
  // correctly either way, since a chosen file always wins there too).
  if (e.target.files[0]) {
    document.getElementById('e-remove-photo').checked = false;
  }
});

document.getElementById('e-remove-photo').addEventListener('change', e => {
  const preview = document.getElementById('e-image-preview');
  if (e.target.checked) {
    document.getElementById('e-image').value = '';
    preview.classList.add('hidden');
  } else {
    const item = wardrobeItemsById[document.getElementById('e-id').value];
    if (item && item.image_url) {
      preview.src = item.image_url;
      preview.classList.remove('hidden');
    }
  }
});

function openEditModal(id) {
  const item = wardrobeItemsById[id];
  if (!item) return;
  clearMsg('edit-msg');
  document.getElementById('e-id').value = id;
  document.getElementById('e-name').value = item.name;
  setColorPickerValue('e', item.color, item.color_hex);
  document.getElementById('e-type').value = item.clothing_type;
  document.getElementById('e-season').value = item.season;
  setMultiSelectValues('e-style', item.style);
  setMultiSelectValues('e-occasion', item.occasion);
  document.getElementById('e-image').value = '';
  document.getElementById('e-remove-photo').checked = false;
  const preview = document.getElementById('e-image-preview');
  const removeRow = document.getElementById('e-remove-photo-row');
  if (item.image_url) {
    preview.src = item.image_url;
    preview.classList.remove('hidden');
    removeRow.classList.remove('hidden');
  } else {
    preview.src = '';
    preview.classList.add('hidden');
    removeRow.classList.add('hidden');
  }
  document.getElementById('edit-modal').classList.remove('hidden');
}

function closeEditModal() {
  document.getElementById('edit-modal').classList.add('hidden');
  document.getElementById('edit-form').reset();
}

document.getElementById('edit-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('edit-msg');

  if (!document.getElementById('e-color').value) {
    showMsg('edit-msg', 'Please choose a color.', 'err');
    return;
  }
  if (!getMultiSelectValues('e-style').length) {
    showMsg('edit-msg', 'Please choose at least one style.', 'err');
    return;
  }
  if (!getMultiSelectValues('e-occasion').length) {
    showMsg('edit-msg', 'Please choose at least one occasion.', 'err');
    return;
  }

  const id = document.getElementById('e-id').value;
  const formData = new FormData();
  formData.append('name',          document.getElementById('e-name').value.trim());
  formData.append('color',         document.getElementById('e-color').value.trim());
  formData.append('color_hex',     document.getElementById('e-color-hex').value.trim());
  formData.append('clothing_type', document.getElementById('e-type').value);
  formData.append('season',        document.getElementById('e-season').value);
  getMultiSelectValues('e-style').forEach(v => formData.append('style', v));
  getMultiSelectValues('e-occasion').forEach(v => formData.append('occasion', v));
  const imageFile = document.getElementById('e-image').files[0];
  if (imageFile) {
    // A chosen replacement always wins over a pending removal, regardless
    // of the checkbox's state - matches the change listeners above, which
    // already keep them in sync, but this is the actual source of truth.
    formData.append('image', imageFile);
  } else if (document.getElementById('e-remove-photo').checked) {
    // An explicit flag, not image_url='' - an empty multipart field
    // is parsed server-side as the parameter being absent, not "", so it
    // could never actually signal removal over real HTTP (see
    // routers/wardrobe.py::update_item's own comment on this).
    formData.append('remove_image', 'true');
  }

  try {
    const res = await fetch(`/wardrobe/${id}`, {
      method: 'PUT',
      headers: authHeadersMultipart(),
      body: formData,
    });
    if (res.ok) {
      closeEditModal();
      loadWardrobe();
    } else {
      const d = await res.json();
      showMsg('edit-msg', formatApiError(d.detail, 'Failed to update item.'), 'err');
    }
  } catch {
    showMsg('edit-msg', 'Network error.', 'err');
  }
});

/* ── Recommendation ─────────────────────────────────────────────── */

let lastRecItemIds = [];

/* Turns the API's missing_types (e.g. ["top","bottom"]) into a plain
   English sentence. Defensive: any non-array or unrecognized value is
   treated as "nothing missing" rather than crashing or showing raw
   enum values. */
function missingTypesMessage(missingTypes) {
  if (!Array.isArray(missingTypes) || missingTypes.length === 0) return '';
  const hasTop = missingTypes.includes('top');
  const hasBottom = missingTypes.includes('bottom');
  if (hasTop && hasBottom) return 'A suitable top and bottom were not found in your wardrobe.';
  if (hasTop) return 'A suitable top was not found in your wardrobe.';
  if (hasBottom) return 'A suitable bottom was not found in your wardrobe.';
  return '';
}

function clearRecMissing() {
  const el = document.getElementById('rec-missing');
  el.textContent = '';
  el.classList.add('hidden');
}

function renderRecMissing(missingTypes) {
  const el = document.getElementById('rec-missing');
  const message = missingTypesMessage(missingTypes);
  if (message) {
    el.textContent = message;
    el.classList.remove('hidden');
  } else {
    el.textContent = '';
    el.classList.add('hidden');
  }
}

/* ── Catalog (crawler) shop suggestions ─────────────────────────────
   Purely additive to the recommendation flow above: fetched only when
   missing_types is non-empty, via the separate GET /recommendations/
   catalog-suggestions endpoint (never folded into the recommendation
   response itself). A catalog failure of any kind (service down, timeout,
   malformed response - see catalog_available in the response) is never
   shown as an error; the section simply stays hidden, exactly like having
   no missing_types at all. */

function clearCatalogSuggestions() {
  const el = document.getElementById('rec-shop-suggestions');
  el.innerHTML = '';
  el.classList.add('hidden');
}

/* Catalog products are crawled third-party data (see services/catalog),
   so every field is treated as untrusted text here: escaped for HTML, and
   - for anything used as a URL - restricted to http(s), since esc() alone
   would happily pass a "javascript:" href through unchanged. A product
   with no usable source_url is dropped rather than rendered as a dead or
   unsafe link. */
function safeHttpUrl(url) {
  const raw = String(url ?? '').trim();
  return /^https?:\/\//i.test(raw) ? raw : '';
}

function renderCatalogProductCard(product) {
  const href = safeHttpUrl(product.source_url);
  if (!href) return '';
  const price = (product.price != null && product.currency)
    ? `${product.price} ${product.currency}`
    : '';
  const imgSrc = safeHttpUrl(product.image_url);
  const img = imgSrc
    ? `<img src="${esc(imgSrc)}" alt="${esc(product.product_name)}" loading="lazy">`
    : '';
  return `
    <a class="shop-card" href="${esc(href)}" target="_blank" rel="noopener noreferrer">
      ${img}
      <div class="shop-card-name">${esc(product.product_name)}</div>
      <div class="shop-card-price">${esc(price)}</div>
    </a>`;
}

/* Fetch-only: no DOM, no container. Shared by the Dashboard's
   recommendation panel below and the Outfit Builder's own section (see
   outfit-builder.js), so the two never write into each other's element.
   Returns one entry per requested type, keeping `available` distinct from
   "reached, but nothing matched" - the backend reports that difference
   via catalog_available and each caller words it differently.

   `season` is optional and comes only from the caller's own current
   recommendation request - never hardcoded, never derived from a returned
   item's own attributes. Passed straight through as-is (the backend owns
   deciding whether explicit evidence or its documented spring->summer/
   autumn->winter fallback satisfies it - this file never re-implements
   that mapping). Omitted entirely (as before this feature existed) when
   the caller doesn't pass one - see outfit-builder.js's own call, which
   never has a recommendation-request season to offer and must keep
   requesting exactly the pre-existing clothing-type-only shape. */
async function fetchCatalogSuggestions(clothingTypes, season) {
  return Promise.all(clothingTypes.map(async (type) => {
    const seasonParam = season ? `&season=${encodeURIComponent(season)}` : '';
    try {
      const res = await fetch(
        `/recommendations/catalog-suggestions?clothing_type=${encodeURIComponent(type)}&max_results=4${seasonParam}`,
        { headers: authHeaders() }
      );
      if (!res.ok) {
        return { type, products: [], available: false, seasonRequested: null, seasonVerified: false };
      }
      const d = await res.json();
      return {
        type,
        products: d.catalog_available ? d.products : [],
        available: !!d.catalog_available,
        // Echoed straight from the response, never assumed from what was
        // sent - season_verified only ever means the backend actually
        // confirmed trusted collection evidence for these specific
        // products, per season_matches().
        seasonRequested: d.season_requested || null,
        seasonVerified: !!d.season_verified,
      };
    } catch {
      return { type, products: [], available: false, seasonRequested: null, seasonVerified: false };
    }
  }));
}

/* One result entry -> its group label. Seasonal wording ("Winter top") is
   only ever used when the backend actually verified trusted season
   evidence for these products (seasonVerified) - a season that was
   requested but fell back to the clothing-type-only result (seasonVerified
   false) keeps the existing generic label, with a small, honest
   "(season not verified)" hint so a type-only fallback is never presented
   as if it were a confirmed seasonal match. No season requested at all
   (e.g. every Outfit Builder call) renders exactly as before this feature
   existed. */
function shopSuggestionsLabel(entry) {
  if (entry.seasonRequested && entry.seasonVerified) {
    const season = entry.seasonRequested;
    const capitalized = season.charAt(0).toUpperCase() + season.slice(1);
    return `${esc(capitalized)} ${esc(entry.type)}`;
  }
  const unverifiedHint = entry.seasonRequested
    ? ' <span class="hint" style="margin:0">(season not verified)</span>'
    : '';
  return `Shop for a ${esc(entry.type)}${unverifiedHint}`;
}

/* Render-only: returns HTML for the groups that actually have products,
   or '' when there are none. Caller decides which element receives it. */
function catalogSuggestionsHtml(results) {
  const withProducts = results.filter(r => r.products.length > 0);
  if (withProducts.length === 0) return '';
  return withProducts.map(r => `
    <div class="shop-suggestions-group">
      <p class="shop-suggestions-label">${shopSuggestionsLabel(r)}</p>
      <div class="shop-suggestions-row">${r.products.map(renderCatalogProductCard).join('')}</div>
    </div>
  `).join('');
}

async function loadCatalogSuggestions(missingTypes, season) {
  clearCatalogSuggestions();
  if (!Array.isArray(missingTypes) || missingTypes.length === 0) return;

  const html = catalogSuggestionsHtml(await fetchCatalogSuggestions(missingTypes, season));
  if (!html) return;

  const el = document.getElementById('rec-shop-suggestions');
  el.innerHTML = html;
  el.classList.remove('hidden');
}

async function getRecommendation(triggerBtn) {
  clearMsg('rec-msg');
  document.getElementById('rec-result').classList.add('hidden');
  clearRecMissing();
  clearCatalogSuggestions();
  lastRecItemIds = [];
  clearMsg('save-outfit-msg');
  clearMsg('rec-feedback-msg');
  document.getElementById('rec-feedback-scope').value = '';
  onRecFeedbackScopeChange();

  const body = {};
  const occasion = document.getElementById('r-occasion').value;
  const season   = document.getElementById('r-season').value;
  const style    = document.getElementById('r-style').value;
  if (occasion) body.occasion = occasion;
  if (season)   body.season   = season;
  if (style)    body.style    = style;

  await withButtonPending(triggerBtn, 'Generating…', async () => {
    try {
      const res = await fetch('/recommendations', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(body),
      });
      const d = await res.json();
      if (res.ok) {
        // d.explanation is intentionally not rendered here - the outfit
        // cards below already show exactly what was selected; a
        // constructed description sentence above them was redundant.
        renderRecMissing(d.missing_types);
        // The user's own current recommendation-request season (may be
        // '' if none was selected) - never hardcoded, never derived from
        // the returned items themselves.
        loadCatalogSuggestions(d.missing_types, season);
        document.getElementById('rec-preview').innerHTML =
          d.items.length
            ? renderOutfitFlatLay(d.items, { editable: false })
            : '<p style="color:var(--muted);font-size:.88rem;padding:4px 0">No items matched your criteria.</p>';
        lastRecItemIds = d.items.map(item => item.id);
        document.getElementById('rec-result').classList.remove('hidden');
      } else {
        showMsg('rec-msg', formatApiError(d.detail, 'Could not get a recommendation.'), 'err');
      }
    } catch {
      showMsg('rec-msg', 'Network error.', 'err');
    }
  });
}

/* ── Save / customize a recommendation ──────────────────────────────
   Both read the same lastRecItemIds + current occasion/season/style
   filters and go through outfit-builder.js's shared persistOutfit -
   the only difference is whether Builder opens first. This retires
   the old standalone saveOutfit()/#rec-outfit-name flow: naming an
   outfit now happens inside Builder (Customize), or afterward via
   Edit from My Outfits. */

function recommendationDefaultName() {
  const today = new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  return `Recommended Outfit — ${today}`;
}

async function saveRecommendedOutfit() {
  clearMsg('save-outfit-msg');

  if (!lastRecItemIds.length) {
    showMsg('save-outfit-msg', 'Generate a recommendation first.', 'err');
    return;
  }

  const result = await persistOutfit({
    itemIds: lastRecItemIds,
    name: recommendationDefaultName(),
    occasion: document.getElementById('r-occasion').value,
    season: document.getElementById('r-season').value,
    style: document.getElementById('r-style').value,
  });

  if (result.ok) {
    showMsg('save-outfit-msg', 'Outfit saved. Rename it anytime from My Outfits.', 'ok');
    loadSavedOutfits();
  } else {
    showMsg('save-outfit-msg', result.error, 'err');
  }
}

function customizeRecommendedOutfit() {
  if (!lastRecItemIds.length) {
    showMsg('save-outfit-msg', 'Generate a recommendation first.', 'err');
    return;
  }
  openOutfitBuilder({
    itemIds: lastRecItemIds,
    occasion: document.getElementById('r-occasion').value,
    season: document.getElementById('r-season').value,
    style: document.getElementById('r-style').value,
  });
}

/* ── Recommendation feedback ────────────────────────────────────────
   Like/dislike a fresh recommendation directly, independent of saving
   it - POST /recommendation-feedback, never /feedback (which requires
   an already-saved outfit). The reason picker stays optional: leaving
   it on "No specific reason" still submits a valid bare like/dislike. */

const REC_FEEDBACK_ITEM_SCOPES = ['color', 'style', 'clothing_type'];

function onRecFeedbackScopeChange() {
  const scope = document.getElementById('rec-feedback-scope').value;
  const itemField = document.getElementById('rec-feedback-item-field');
  const itemSelect = document.getElementById('rec-feedback-item');

  if (REC_FEEDBACK_ITEM_SCOPES.includes(scope)) {
    itemSelect.innerHTML = lastRecItemIds
      .map(id => {
        const item = wardrobeItemsById[id];
        const label = item ? `${EMOJI[item.clothing_type] || ''} ${item.name}` : id;
        return `<option value="${esc(id)}">${esc(label)}</option>`;
      })
      .join('');
    itemField.classList.remove('hidden');
  } else {
    itemField.classList.add('hidden');
    itemSelect.innerHTML = '';
  }
}

async function submitRecFeedback(liked) {
  clearMsg('rec-feedback-msg');

  if (!lastRecItemIds.length) {
    showMsg('rec-feedback-msg', 'Generate a recommendation first.', 'err');
    return;
  }

  const scope = document.getElementById('rec-feedback-scope').value;
  const body = { item_ids: lastRecItemIds, liked };
  if (scope) body.reason_scope = scope;
  if (REC_FEEDBACK_ITEM_SCOPES.includes(scope)) {
    const itemId = document.getElementById('rec-feedback-item').value;
    if (!itemId) {
      showMsg('rec-feedback-msg', 'Choose which item this reason refers to.', 'err');
      return;
    }
    body.reason_item_id = itemId;
  }
  const occasion = document.getElementById('r-occasion').value;
  const season   = document.getElementById('r-season').value;
  const style     = document.getElementById('r-style').value;
  if (occasion) body.occasion = occasion;
  if (season)   body.season   = season;
  if (style)    body.style    = style;

  try {
    const res = await fetch('/recommendation-feedback', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify(body),
    });
    if (res.status === 201) {
      showMsg('rec-feedback-msg', liked ? 'Thanks - noted as liked.' : 'Thanks - noted as disliked.', 'ok');
      document.getElementById('rec-feedback-scope').value = '';
      onRecFeedbackScopeChange();
    } else {
      const d = await res.json();
      showMsg('rec-feedback-msg', formatApiError(d.detail, 'Could not record feedback.'), 'err');
    }
  } catch {
    showMsg('rec-feedback-msg', 'Network error.', 'err');
  }
}

/* ── Saved outfits ──────────────────────────────────────────────── */

function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

function outfitCard(outfit) {
  const badges = [
    outfit.occasion ? `<span class="badge occasion">${esc(outfit.occasion)}</span>` : '',
    outfit.season   ? `<span class="badge season">${esc(outfit.season)}</span>`     : '',
    outfit.style    ? `<span class="badge style">${esc(outfit.style)}</span>`       : '',
  ].join('');

  return `
    <div class="outfit-card" id="outfit-${esc(outfit.id)}">
      <div class="outfit-card-header">
        <div class="outfit-name">${esc(outfit.name || 'Untitled Outfit')}</div>
        <div class="outfit-date">${formatDate(outfit.created_at)}</div>
      </div>
      ${badges ? `<div class="badges">${badges}</div>` : ''}
      ${renderOutfitFlatLay(outfit.items, { editable: false })}
      ${feedbackBlock(outfit)}
      <div class="outfit-actions">
        <button class="btn btn-ghost" onclick="openOutfitBuilderForEdit('${esc(outfit.id)}')">Edit</button>
        <button class="btn btn-ghost" onclick="shareOutfitToExplore('${esc(outfit.id)}')">Share to Explore</button>
        <button class="btn btn-danger" onclick="deleteSavedOutfit('${esc(outfit.id)}')">Delete</button>
      </div>
    </div>`;
}

/* ── Outfit feedback ────────────────────────────────────────────── */

let feedbackByOutfit = {};

function starDisplay(rating) {
  let html = '';
  for (let i = 1; i <= 5; i++) {
    html += `<span class="star ${i <= rating ? 'filled' : ''}">&#9733;</span>`;
  }
  return html;
}

function feedbackBlock(outfit) {
  const fb = feedbackByOutfit[outfit.id];
  return `
    <div class="feedback-block">
      <div class="feedback-display" id="feedback-display-${esc(outfit.id)}">
        ${fb ? `
          <div class="feedback-summary">
            <span class="star-display">${starDisplay(fb.rating)}</span>
            ${fb.comment ? `<span class="feedback-comment">&ldquo;${esc(fb.comment)}&rdquo;</span>` : ''}
          </div>
          <div class="feedback-actions">
            <button class="btn btn-ghost btn-sm" onclick="toggleFeedbackForm('${esc(outfit.id)}')">Edit Rating</button>
            <button class="btn btn-danger btn-sm" onclick="deleteFeedback('${esc(outfit.id)}', '${esc(fb.id)}')">Remove</button>
          </div>
        ` : `
          <button class="btn btn-ghost btn-sm" onclick="toggleFeedbackForm('${esc(outfit.id)}')">Rate this outfit</button>
        `}
      </div>
      ${feedbackForm(outfit, fb)}
    </div>`;
}

function feedbackForm(outfit, fb) {
  const rating = fb ? fb.rating : 0;
  const comment = fb ? (fb.comment || '') : '';
  const stars = [1, 2, 3, 4, 5].map(n =>
    `<button type="button" class="star-btn ${n <= rating ? 'filled' : ''}" data-value="${n}"
       onclick="selectStar('${esc(outfit.id)}', ${n})">&#9733;</button>`
  ).join('');
  return `
    <div class="feedback-form hidden" id="feedback-form-${esc(outfit.id)}">
      <div class="star-rating" id="star-rating-${esc(outfit.id)}" data-rating="${rating}">${stars}</div>
      <textarea id="feedback-comment-${esc(outfit.id)}" class="feedback-textarea"
                placeholder="Optional comment..." maxlength="500">${esc(comment)}</textarea>
      <div class="feedback-form-actions">
        <button class="btn btn-primary btn-sm" onclick="submitFeedback('${esc(outfit.id)}')">${fb ? 'Update' : 'Submit'} Rating</button>
        <button class="btn btn-ghost btn-sm" onclick="toggleFeedbackForm('${esc(outfit.id)}')">Cancel</button>
      </div>
    </div>`;
}

function toggleFeedbackForm(outfitId) {
  document.getElementById(`feedback-display-${outfitId}`).classList.toggle('hidden');
  document.getElementById(`feedback-form-${outfitId}`).classList.toggle('hidden');
}

function selectStar(outfitId, value) {
  const container = document.getElementById(`star-rating-${outfitId}`);
  container.dataset.rating = value;
  container.querySelectorAll('.star-btn').forEach(btn => {
    btn.classList.toggle('filled', Number(btn.dataset.value) <= value);
  });
}

async function submitFeedback(outfitId) {
  clearMsg('feedback-msg');
  const rating = Number(document.getElementById(`star-rating-${outfitId}`).dataset.rating);
  if (!rating) {
    showMsg('feedback-msg', 'Please select a star rating.', 'err');
    return;
  }
  const comment = document.getElementById(`feedback-comment-${outfitId}`).value.trim();
  const existing = feedbackByOutfit[outfitId];

  try {
    let res;
    if (existing) {
      res = await fetch(`/feedback/${existing.id}`, {
        method: 'PUT',
        headers: authHeaders(),
        body: JSON.stringify({ rating, comment: comment || null }),
      });
    } else {
      res = await fetch('/feedback', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ outfit_id: outfitId, rating, comment: comment || null }),
      });
    }
    if (res.ok) {
      showMsg('feedback-msg', existing ? 'Rating updated.' : 'Rating saved.', 'ok');
      loadSavedOutfits();
    } else {
      const d = await res.json();
      showMsg('feedback-msg', formatApiError(d.detail, 'Failed to save rating.'), 'err');
    }
  } catch {
    showMsg('feedback-msg', 'Network error.', 'err');
  }
}

async function deleteFeedback(outfitId, feedbackId) {
  if (!confirm('Remove your rating for this outfit?')) return;
  clearMsg('feedback-msg');
  try {
    const res = await fetch(`/feedback/${feedbackId}`, {
      method: 'DELETE',
      headers: authHeaders(),
    });
    if (res.status === 204) {
      loadSavedOutfits();
    } else {
      showMsg('feedback-msg', 'Failed to remove rating.', 'err');
    }
  } catch {
    showMsg('feedback-msg', 'Network error.', 'err');
  }
}

async function loadSavedOutfits() {
  try {
    const [outfitsRes, feedbackRes] = await Promise.all([
      fetch('/outfits', { headers: authHeaders() }),
      fetch('/feedback', { headers: authHeaders() }),
    ]);
    if (!outfitsRes.ok) return;
    const outfits = await outfitsRes.json();
    feedbackByOutfit = {};
    if (feedbackRes.ok) {
      const feedbackList = await feedbackRes.json();
      feedbackList.forEach(f => { feedbackByOutfit[f.outfit_id] = f; });
    }
    renderSavedOutfits(outfits);
  } catch { /* silent on load */ }
}

let savedOutfitsById = {};

function renderSavedOutfits(outfits) {
  const grid  = document.getElementById('saved-outfits-grid');
  const empty = document.getElementById('saved-outfits-empty');
  savedOutfitsById = {};
  outfits.forEach(outfit => { savedOutfitsById[outfit.id] = outfit; });
  document.getElementById('stat-outfits-count').textContent = outfits.length;
  if (outfits.length === 0) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    grid.innerHTML = outfits.map(outfitCard).join('');
  }
}

/* Edit reopens the same Builder used for creation, pre-filled from the
   cached outfit - see openOutfitBuilder's editingOutfitId branch in
   outfit-builder.js, which PATCHes instead of POSTing on save. */
function openOutfitBuilderForEdit(outfitId) {
  const outfit = savedOutfitsById[outfitId];
  if (!outfit) {
    showMsg('saved-outfits-msg', 'Could not find that outfit - try refreshing.', 'err');
    return;
  }
  openOutfitBuilder({
    itemIds: outfit.item_ids,
    name: outfit.name,
    occasion: outfit.occasion,
    season: outfit.season,
    style: outfit.style,
    editingOutfitId: outfit.id,
  });
}

/* Reuses the existing Explore post-creation outfit picker (shared_outfit_id)
   rather than introducing a second sharing mechanism - just navigates
   there and pre-selects this outfit. loadOutfitsForPicker() is normally
   only fetched once, the first time Explore is opened (see
   explore-core.js::showExploreTab), so it's called again here
   unconditionally to guarantee this outfit's option actually exists
   before we try to select it - otherwise an outfit created or edited
   after that first visit would be missing from a stale dropdown. */
async function shareOutfitToExplore(outfitId) {
  showExploreTab();
  await loadOutfitsForPicker();
  const select = document.getElementById('post-shared-outfit');
  select.value = outfitId;
  document.getElementById('post-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
  document.getElementById('post-title').focus();
}

async function deleteSavedOutfit(id) {
  if (!confirm('Delete this saved outfit?')) return;
  clearMsg('saved-outfits-msg');
  try {
    const res = await fetch(`/outfits/${id}`, {
      method: 'DELETE',
      headers: authHeaders(),
    });
    if (res.status === 204) {
      loadSavedOutfits();
    } else {
      showMsg('saved-outfits-msg', 'Failed to delete outfit.', 'err');
    }
  } catch {
    showMsg('saved-outfits-msg', 'Network error.', 'err');
  }
}

/* ── Style Memory ───────────────────────────────────────────────── */

function parseList(str) {
  return str.split(',').map(s => s.trim()).filter(Boolean);
}

function populateMemoryForm(mem) {
  document.getElementById('m-preferred-styles').value = (mem.preferred_styles || []).join(', ');
  document.getElementById('m-preferred-occasions').value = (mem.preferred_occasions || []).join(', ');
  document.getElementById('m-liked-colors').value = (mem.liked_colors || []).join(', ');
  document.getElementById('m-disliked-colors').value = (mem.disliked_colors || []).join(', ');
  document.getElementById('m-disliked-item-types').value = (mem.disliked_item_types || []).join(', ');
  document.getElementById('m-preferred-fit').value = mem.preferred_fit || '';
  document.getElementById('m-communication-style').value = mem.communication_style || '';
  document.getElementById('m-notes').value = mem.notes || '';
}

async function loadMemory() {
  try {
    const res = await fetch('/memory', { headers: authHeaders() });
    if (!res.ok) return;
    const mem = await res.json();
    populateMemoryForm(mem);
  } catch { /* silent on load */ }
}

document.getElementById('memory-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('memory-msg');

  const body = {
    preferred_styles: parseList(document.getElementById('m-preferred-styles').value),
    preferred_occasions: parseList(document.getElementById('m-preferred-occasions').value),
    liked_colors: parseList(document.getElementById('m-liked-colors').value),
    disliked_colors: parseList(document.getElementById('m-disliked-colors').value),
    disliked_item_types: parseList(document.getElementById('m-disliked-item-types').value),
    preferred_fit: document.getElementById('m-preferred-fit').value || null,
    communication_style: document.getElementById('m-communication-style').value || null,
    notes: document.getElementById('m-notes').value.trim() || null,
  };

  const submitBtn = e.target.querySelector('button[type="submit"]');
  await withButtonPending(submitBtn, 'Saving…', async () => {
    try {
      const res = await fetch('/memory', {
        method: 'PUT',
        headers: authHeaders(),
        body: JSON.stringify(body),
      });
      if (res.ok) {
        showMsg('memory-msg', 'Preferences saved.', 'ok');
      } else {
        const d = await res.json();
        showMsg('memory-msg', formatApiError(d.detail, 'Failed to save preferences.'), 'err');
      }
    } catch {
      showMsg('memory-msg', 'Network error.', 'err');
    }
  });
});

/* ── Start ──────────────────────────────────────────────────────── */

boot();
