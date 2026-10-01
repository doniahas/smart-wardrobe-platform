/* ── Outfit visualization + Builder ────────────────────────────────
   renderOutfitFlatLay() is the one shared layout used both here, in
   Builder's editable live preview, and in read-only mode by both the
   Recommendation section's result and My Outfits' cards (app.js) -
   the two modes differ only in whether a tile gets a remove
   affordance and whether an empty region renders an "add"
   placeholder or is omitted entirely.

   Builder itself is the single creation/edit surface for all three
   entry points - see builderMode below. Saving always goes through
   the existing, already-validated POST/PATCH /outfits endpoints. */

/* clothing_type keys grouped into visual regions. bottom/dress share
   one region since a dress is mutually exclusive with a bottom (and,
   separately, with every top - see builderToggleItem below) - only
   one of bottom/dress is ever populated at a time, so showing them
   as two permanently separate regions would look like both could be
   filled together. Tops keep their own region since a dress doesn't
   change whether tops themselves are shown - it just means that
   region will be empty whenever a dress is selected. */
const OUTFIT_REGIONS = [
  { key: 'outerwear', label: 'Outerwear', types: ['outerwear'] },
  { key: 'top', label: 'Tops', types: ['top'] },
  { key: 'bottom_dress', label: 'Bottom / Dress', types: ['bottom', 'dress'] },
  { key: 'shoes', label: 'Shoes', types: ['shoes'] },
  { key: 'accessory', label: 'Accessories', types: ['accessory'] },
];

/* Per-type rules mirrored from routers/outfits.py::_validate_outfit_items
   for immediate UX feedback. The server remains the actual authority -
   this is a convenience layer, not a trust boundary; a mismatch here
   would simply surface as the existing d.detail error message on save. */
const MAX_ONE_TYPES = ['bottom', 'shoes', 'dress'];

function _capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function groupItemsByType(items) {
  const grouped = {};
  items.forEach(item => {
    (grouped[item.clothing_type] = grouped[item.clothing_type] || []).push(item);
  });
  return grouped;
}

/* ── Shared flat-lay renderer ───────────────────────────────────── */

function flatLayTile(item, editable) {
  const emoji = EMOJI[item.clothing_type] || '👔';
  const thumb = item.image_url
    ? `<img src="${esc(item.image_url)}" alt="${esc(item.name)}"
         onerror="this.outerHTML='<span class=\\'flatlay-tile-emoji\\'>${emoji}</span>'">`
    : `<span class="flatlay-tile-emoji">${emoji}</span>`;
  const removeBtn = editable
    ? `<button type="button" class="flatlay-tile-remove" title="Remove ${esc(item.name)}"
         onclick="builderToggleItem('${esc(item.id)}')">&times;</button>`
    : '';
  return `
    <div class="flatlay-tile" title="${esc(item.name)}">
      ${thumb}
      ${removeBtn}
    </div>`;
}

function flatLayPlaceholder(region) {
  return `
    <button type="button" class="flatlay-placeholder" onclick="focusPickerGroup('${region.key}')">
      + Add ${esc(region.label.toLowerCase())}
    </button>`;
}

/* items: array of WardrobeItemPublic-shaped objects currently in the
   outfit. editable: true inside Builder (adds remove buttons and
   clickable empty-region placeholders); false for any future
   read-only display, where an empty region is simply omitted. */
function renderOutfitFlatLay(items, { editable = false } = {}) {
  const grouped = groupItemsByType(items);
  const regionsHtml = OUTFIT_REGIONS.map(region => {
    const regionItems = region.types.flatMap(t => grouped[t] || []);
    if (regionItems.length === 0) {
      if (!editable) return '';
      return `<div class="flatlay-region flatlay-region-empty">${flatLayPlaceholder(region)}</div>`;
    }
    return `
      <div class="flatlay-region">
        <div class="flatlay-region-label">${esc(region.label)}</div>
        <div class="flatlay-region-tiles">
          ${regionItems.map(item => flatLayTile(item, editable)).join('')}
        </div>
      </div>`;
  }).join('');
  return `<div class="flatlay">${regionsHtml || '<p class="hint">No items selected yet.</p>'}</div>`;
}

/* ── Builder state ──────────────────────────────────────────────────
   builderMode/builderEditingOutfitId distinguish the three ways
   Builder can be opened, all through the same openOutfitBuilder():
     - openOutfitBuilder()                         -> empty create
     - openOutfitBuilder({ itemIds, occasion, ... }) -> recommendation-
       prefilled create (no editingOutfitId - still a POST on save)
     - openOutfitBuilder({ ..., editingOutfitId })  -> edit (PATCH) */

let builderSelectedIds = new Set();
let builderMode = 'create';
let builderEditingOutfitId = null;

/* ── Catalog shop suggestions (Recreate handoff) ───────────────────
   Optional context from Recreate: the clothing types it could not match
   in this user's wardrobe (see explore-posts.js). Purely advisory - an
   external product is never added to the wardrobe or the outfit, and
   nothing here is ever saved. Every other caller of openOutfitBuilder()
   (new outfit, edit, recommendation customize) passes no missingTypes,
   so this section stays empty and makes no request for them.

   builderCatalogRequestId guards against a slow response from a previous
   open landing in a Builder that has since been reopened for something
   else - only the newest request may render. */
let builderCatalogRequestId = 0;

function clearBuilderShopSuggestions() {
  const el = document.getElementById('builder-shop-suggestions');
  el.innerHTML = '';
  el.classList.add('hidden');
}

async function renderBuilderShopSuggestions(missingTypes) {
  clearBuilderShopSuggestions();
  if (!Array.isArray(missingTypes) || missingTypes.length === 0) return;

  const requestId = ++builderCatalogRequestId;
  /* Reuses the shared, container-agnostic catalog helpers in app.js -
     the same verified GET /recommendations/catalog-suggestions endpoint
     the Dashboard uses, never a different source of products. */
  const results = await fetchCatalogSuggestions(missingTypes);
  if (requestId !== builderCatalogRequestId) return;

  const el = document.getElementById('builder-shop-suggestions');
  const productsHtml = catalogSuggestionsHtml(results);

  let body;
  if (productsHtml) {
    body = productsHtml;
  } else if (results.some(r => !r.available)) {
    /* Reached nothing / timed out / unusable response - say so plainly
       rather than implying these stores stock nothing. */
    body = '<p class="hint">Shop suggestions unavailable.</p>';
  } else {
    body = '<p class="hint">No suggestions found for the missing items.</p>';
  }

  el.innerHTML =
    `<p class="shop-suggestions-label">Not in your wardrobe: ${missingTypes.map(esc).join(', ')}</p>${body}`;
  el.classList.remove('hidden');
}

function openOutfitBuilder(prefill) {
  builderMode = prefill && prefill.editingOutfitId ? 'edit' : 'create';
  builderEditingOutfitId = (prefill && prefill.editingOutfitId) || null;
  builderSelectedIds = new Set((prefill && prefill.itemIds) || []);

  clearMsg('builder-msg');
  document.getElementById('builder-name').value = (prefill && prefill.name) || '';
  document.getElementById('builder-occasion').value = (prefill && prefill.occasion) || '';
  document.getElementById('builder-season').value = (prefill && prefill.season) || '';
  document.getElementById('builder-style').value = (prefill && prefill.style) || '';

  document.getElementById('builder-modal-title').textContent =
    builderMode === 'edit' ? 'Edit Outfit' : 'Build an Outfit';
  document.getElementById('builder-save-btn').textContent =
    builderMode === 'edit' ? 'Save Changes' : 'Save Outfit';

  renderBuilderPicker();
  renderBuilderPreview();
  document.getElementById('outfit-builder-modal').classList.remove('hidden');

  /* Deliberately not awaited: the Builder is fully usable immediately,
     and a slow or failing catalog call must never delay or block it. */
  renderBuilderShopSuggestions(prefill && prefill.missingTypes);
}

function closeOutfitBuilder() {
  document.getElementById('outfit-builder-modal').classList.add('hidden');
}

function renderBuilderPreview() {
  const items = [...builderSelectedIds].map(id => wardrobeItemsById[id]).filter(Boolean);
  document.getElementById('builder-preview').innerHTML = renderOutfitFlatLay(items, { editable: true });
  document.getElementById('builder-save-btn').disabled = builderSelectedIds.size === 0;
}

/* ── Wardrobe picker (grouped by the same regions as the flat-lay,
   so focusPickerGroup can scroll/emphasize a region by the same key
   an empty-region placeholder was clicked with) ────────────────── */

function builderPickerTile(item) {
  const selected = builderSelectedIds.has(item.id);
  const emoji = EMOJI[item.clothing_type] || '👔';
  const thumb = item.image_url
    ? `<img src="${esc(item.image_url)}" alt="${esc(item.name)}">`
    : `<span class="picker-tile-emoji">${emoji}</span>`;
  return `
    <button type="button" class="picker-tile ${selected ? 'selected' : ''}"
            onclick="builderToggleItem('${esc(item.id)}')">
      ${thumb}
      <span class="picker-tile-name">${esc(item.name)}</span>
    </button>`;
}

function renderBuilderPicker() {
  const container = document.getElementById('builder-picker');
  const grouped = groupItemsByType(Object.values(wardrobeItemsById));

  container.innerHTML = OUTFIT_REGIONS.map(region => {
    const subgroupsHtml = region.types.map(type => {
      const items = grouped[type] || [];
      const label = _capitalize(type);
      const tiles = items.length
        ? items.map(builderPickerTile).join('')
        : `<p class="hint">No ${esc(label.toLowerCase())} in your wardrobe yet.</p>`;
      return `
        <div class="builder-picker-subgroup">
          ${region.types.length > 1 ? `<div class="builder-picker-subgroup-label">${esc(label)}</div>` : ''}
          <div class="builder-picker-tiles">${tiles}</div>
        </div>`;
    }).join('');
    return `
      <div class="builder-picker-region" id="builder-picker-region-${region.key}">
        <div class="builder-picker-region-label">${esc(region.label)}</div>
        ${subgroupsHtml}
      </div>`;
  }).join('');
}

function focusPickerGroup(regionKey) {
  const el = document.getElementById(`builder-picker-region-${regionKey}`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('emphasized');
  setTimeout(() => el.classList.remove('emphasized'), 1200);
}

/* ── Selection toggle ───────────────────────────────────────────────
   A true duplicate (the same item id twice) is structurally
   unreachable here since each wardrobe item appears in the picker
   exactly once and selection is a Set, not an appendable list.

   Per-type "max one" (bottom/shoes/dress) and the dress exclusion are
   both handled as an auto-replace rather than a blocking error - the
   same outcome the user almost always wants anyway:
     - selecting a dress clears every selected top and the bottom
       (a dress is mutually exclusive with both, matching the
       recommendation engine's own semantics - see
       routers/outfits.py::_validate_outfit_items);
     - selecting any top or the bottom clears a selected dress;
     - selecting a second bottom/shoes/dress replaces the first. */
function builderToggleItem(itemId) {
  const item = wardrobeItemsById[itemId];
  if (!item) return;

  if (builderSelectedIds.has(itemId)) {
    builderSelectedIds.delete(itemId);
  } else {
    const clearTypes = new Set();
    if (MAX_ONE_TYPES.includes(item.clothing_type)) clearTypes.add(item.clothing_type);
    if (item.clothing_type === 'dress') {
      clearTypes.add('top');
      clearTypes.add('bottom');
    }
    if (item.clothing_type === 'top' || item.clothing_type === 'bottom') {
      clearTypes.add('dress');
    }

    if (clearTypes.size > 0) {
      [...builderSelectedIds].forEach(id => {
        const existing = wardrobeItemsById[id];
        if (existing && clearTypes.has(existing.clothing_type)) {
          builderSelectedIds.delete(id);
        }
      });
    }
    builderSelectedIds.add(itemId);
  }
  renderBuilderPicker();
  renderBuilderPreview();
}

/* ── Save ───────────────────────────────────────────────────────────
   Single shared save path for every outfit-writing entry point in the
   app: Builder's own submit handler below, and the Recommendation
   section's "Save recommended outfit" fast path (app.js), which never
   opens Builder at all. editingOutfitId absent -> POST /outfits
   (create); present -> PATCH /outfits/{id} (edit) - the exact same
   distinction Builder itself uses internally via builderMode. Blank
   name/occasion/season/style are omitted from the request body rather
   than sent as empty strings, so an edit leaves an already-set field
   untouched instead of clearing it (PATCH treats an omitted field as
   "unchanged" - see routers/outfits.py's PATCH semantics). */
async function persistOutfit({ itemIds, name, occasion, season, style, editingOutfitId }) {
  const body = { item_ids: itemIds };
  if (name)     body.name     = name;
  if (occasion) body.occasion = occasion;
  if (season)   body.season   = season;
  if (style)    body.style    = style;

  const url = editingOutfitId ? `/outfits/${editingOutfitId}` : '/outfits';
  const method = editingOutfitId ? 'PATCH' : 'POST';
  const expectedStatus = editingOutfitId ? 200 : 201;

  try {
    const res = await fetch(url, { method, headers: authHeaders(), body: JSON.stringify(body) });
    if (res.status === expectedStatus) {
      return { ok: true, data: await res.json() };
    }
    const d = await res.json().catch(() => ({}));
    return { ok: false, error: formatApiError(d.detail, 'Failed to save outfit.') };
  } catch {
    return { ok: false, error: 'Network error.' };
  }
}

document.getElementById('builder-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearMsg('builder-msg');

  if (builderSelectedIds.size === 0) {
    showMsg('builder-msg', 'Select at least one item.', 'err');
    return;
  }

  const result = await persistOutfit({
    itemIds: [...builderSelectedIds],
    name: document.getElementById('builder-name').value.trim(),
    occasion: document.getElementById('builder-occasion').value,
    season: document.getElementById('builder-season').value,
    style: document.getElementById('builder-style').value,
    editingOutfitId: builderEditingOutfitId,
  });

  if (result.ok) {
    closeOutfitBuilder();
    loadSavedOutfits();
  } else {
    showMsg('builder-msg', result.error, 'err');
  }
});
