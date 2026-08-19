(() => {
  'use strict';

  const app = document.getElementById('review-app');
  const State = window.DistillFeedReviewState;
  if (!app || !State) return;

  let csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const scope = {
    groupId: Number(document.querySelector('meta[name="selected-group-id"]')?.content || 0),
    feedId: Number(document.querySelector('meta[name="selected-feed-id"]')?.content || 0),
    preferenceGroupId: Number(document.querySelector('meta[name="review-preference-group-id"]')?.content || 0),
    title: document.getElementById('items-scope-title')?.textContent?.trim() || '',
  };
  const defaultMinAI = Number(app.dataset.defaultMinAi || 70);
  const today = new Date().toISOString().slice(0, 10);
  const initialDisplayMode = State.normalizeDisplayMode(
    document.querySelector('meta[name="review-display-mode"]')?.content || 'daily',
  );
  const isArxivScope = document.querySelector('meta[name="arxiv-digest-scope"]')?.content === 'true';
  const configuredDefaultPreset = document.querySelector('meta[name="review-default-preset"]')?.content || '';
  const configuredDefaultSort = document.querySelector('meta[name="review-default-sort"]')?.content || '';
  const options = {
    defaultMinAI, today, displayMode: initialDisplayMode, isArxiv: isArxivScope,
    defaultPreset: configuredDefaultPreset, defaultSort: configuredDefaultSort,
  };
  const filterNames = ['preset', ...State.FILTER_FIELDS];
  const params = new URLSearchParams(location.search);
  const rawFilters = {};
  filterNames.forEach(name => { if (params.has(name)) rawFilters[name] = params.get(name); });
  if (!Object.keys(rawFilters).length) rawFilters.preset = State.defaultPreset(options);

  const openStorageKey = `distillfeedReviewOpen:${scope.feedId ? `feed:${scope.feedId}` : `group:${scope.groupId}`}`;
  let restoredOpenDays = {};
  try {
    const parsed = JSON.parse(localStorage.getItem(openStorageKey) || '{}');
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) restoredOpenDays = parsed;
  } catch (_) { restoredOpenDays = {}; }

  let state = { ...State.initialState(rawFilters, options), openDays: restoredOpenDays };
  let daysController = null;
  const dayControllers = new Map();
  const itemControllers = new Map();
  let searchTimer = null;
  let toastTimer = null;
  let requestSequence = 0;
  let directLoadFrame = 0;
  const DIRECT_LOAD_MARGIN = 600;
  const DIRECT_LOAD_CONCURRENCY = 3;

  function nextRequestId(prefix) {
    requestSequence += 1;
    return `${prefix}-${requestSequence}`;
  }

  const daysRoot = document.getElementById('review-day-list');
  const resultStatus = document.getElementById('review-result-status');
  const activeFilters = document.getElementById('review-active-filters');
  const presetControl = document.getElementById('review-preset');
  const searchControl = document.getElementById('review-search');
  const minAIControl = document.getElementById('review-min-ai');
  const sortControl = document.getElementById('review-sort');
  const pageSizeControl = document.getElementById('review-page-size');
  const readControl = document.getElementById('review-read');
  const savedControl = document.getElementById('review-saved');
  const aiControl = document.getElementById('review-ai-state');
  const decisionControl = document.getElementById('review-decision');
  const sourceControl = document.getElementById('review-source');
  const fromControl = document.getElementById('review-from');
  const toControl = document.getElementById('review-to');
  const displayControl = document.getElementById('review-display-mode');
  const displayControlLabel = displayControl?.closest('.review-layout-control');
  const toast = document.getElementById('toast');

  function notify(message) {
    if (!toast) return;
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add('visible');
    toastTimer = setTimeout(() => toast.classList.remove('visible'), 4500);
  }

  async function requestJSON(path, options = {}, retryCSRF = true) {
    const headers = { ...(options.headers || {}) };
    if (csrf) headers['X-CSRF-Token'] = csrf;
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers });
    if (response.status === 403 && retryCSRF) {
      const fresh = await fetch('/api/csrf', { cache: 'no-store' });
      if (fresh.ok) {
        csrf = (await fresh.json()).csrf_token || '';
        return requestJSON(path, options, false);
      }
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || payload.message || `Request failed (${response.status})`);
    return payload;
  }

  function transition(action, render = true) {
    state = State.reducer(state, action);
    if (render) renderReview();
  }

  function scopeParams(target) {
    if (scope.feedId) target.set('feed_id', String(scope.feedId));
    else if (scope.groupId) target.set('group_id', String(scope.groupId));
  }

  function filtersParams(includePageSize = true) {
    const query = new URLSearchParams();
    Object.entries(state.filters).forEach(([key, value]) => {
      if (!includePageSize && key === 'page_size') return;
      if (value !== '' && value !== null && value !== undefined) query.set(key, String(value));
    });
    return query;
  }

  function syncURL() {
    const url = new URL(location.href);
    filterNames.forEach(name => url.searchParams.delete(name));
    Object.entries(state.filters).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) url.searchParams.set(key, String(value));
    });
    history.replaceState(null, '', url);
  }

  function persistOpenDays() {
    const available = new Set(state.days.slice(0, 400).map(day => String(day.day || '')));
    const compact = Object.fromEntries(
      Object.entries(state.openDays)
        .filter(([day, open]) => available.has(day) && typeof open === 'boolean'),
    );
    try { localStorage.setItem(openStorageKey, JSON.stringify(compact)); } catch (_) { /* private mode */ }
  }

  function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>'"]/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    }[character]));
  }

  function safeHref(value) {
    try {
      const url = new URL(String(value || ''), location.origin);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_) { return ''; }
  }

  function formatDay(day) {
    if (day === 'undated') return 'Undated';
    const date = new Date(`${day}T12:00:00Z`);
    if (Number.isNaN(date.getTime())) return day;
    const current = new Date(`${today}T12:00:00Z`);
    const difference = Math.round((current - date) / 86400000);
    const prefix = difference === 0 ? 'Today' : difference === 1 ? 'Yesterday' : '';
    const formatted = new Intl.DateTimeFormat(undefined, {
      weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
    }).format(date);
    return prefix ? `${prefix} · ${formatted}` : formatted;
  }

  function formatTimestamp(value) {
    const date = new Date(value || '');
    if (Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat(undefined, {
      hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    }).format(date);
  }

  function queryLabel(filters) {
    const labels = [];
    if (filters.q) labels.push(`Search: ${filters.q}`);
    if (filters.read !== 'all') labels.push(filters.read === 'unread' ? 'Unread' : 'Read');
    if (filters.saved !== 'all') labels.push(filters.saved === 'starred' ? 'Starred' : 'Read later');
    if (filters.ai !== 'all') labels.push({ scored: 'AI scored', pending: 'Awaiting AI', 'not-sent': 'Not sent to AI' }[filters.ai]);
    if (filters.decision !== 'all') labels.push(filters.decision === 'keep' ? 'AI kept' : 'AI dropped');
    if (filters.min_ai) labels.push(`AI ≥ ${filters.min_ai}`);
    if (filters.source) labels.push(sourceControl?.selectedOptions[0]?.textContent || `Source ${filters.source}`);
    if (filters.from) labels.push(`From ${filters.from}`);
    if (filters.to) labels.push(`To ${filters.to}`);
    return labels;
  }

  function syncControls() {
    if (presetControl) presetControl.value = state.filters.preset;
    if (searchControl && searchControl.value !== state.filters.q) searchControl.value = state.filters.q;
    if (minAIControl) minAIControl.value = String(state.filters.min_ai);
    if (sortControl) sortControl.value = state.filters.sort;
    if (pageSizeControl) pageSizeControl.value = String(state.filters.page_size);
    if (readControl) readControl.value = state.filters.read;
    if (savedControl) savedControl.value = state.filters.saved;
    if (aiControl) aiControl.value = state.filters.ai;
    if (decisionControl) decisionControl.value = state.filters.decision;
    if (fromControl) fromControl.value = state.filters.from;
    if (toControl) toControl.value = state.filters.to;
    if (displayControl) displayControl.value = state.displayMode;
    if (sourceControl) {
      const selected = String(state.filters.source);
      sourceControl.replaceChildren(new Option('All sources', '0'));
      state.sources.forEach(source => sourceControl.add(new Option(`${source.title} (${source.total})`, String(source.id))));
      sourceControl.value = [...sourceControl.options].some(option => option.value === selected) ? selected : '0';
    }
    if (activeFilters) {
      const labels = queryLabel(state.filters);
      activeFilters.replaceChildren(...labels.map(label => {
        const chip = document.createElement('span'); chip.className = 'review-filter-chip'; chip.textContent = label; return chip;
      }));
      activeFilters.hidden = labels.length === 0;
    }
  }

  function cohortLabel(item) {
    if (item.ai_state === 'scored' && item.decision === 'keep') return 'AI-ranked · kept';
    if (item.ai_state === 'scored' && item.decision === 'drop') return 'AI-ranked · dropped';
    if (item.ai_state === 'scored') return 'AI-ranked';
    if (item.ai_state === 'pending') return 'Awaiting AI';
    return item.is_arxiv ? 'Not sent to AI' : 'Without AI score';
  }

  function scoreLabel(item) {
    if (item.ai_state === 'scored') return `AI ${item.ai_score}`;
    if (item.ai_state === 'pending') return 'AI pending';
    return item.is_arxiv ? 'Not sent' : '';
  }

  function detailSection(title, htmlValue, className = '') {
    if (!htmlValue) return '';
    return `<section class="review-detail-section ${className}"><h4>${escapeHTML(title)}</h4><div class="review-markdown">${htmlValue}</div></section>`;
  }

  function renderItemDetails(itemId) {
    const detail = state.itemDetails[itemId];
    if (!detail || detail.status === 'loading') return '<div class="review-detail-loading">Loading item details…</div>';
    if (detail.status === 'error') return `<div class="review-error">${escapeHTML(detail.error)}</div>`;
    const data = detail.data || {};
    return [
      detailSection('AI item summary', data.summary_html),
      detailSection('AI relevance rationale', data.rationale_html),
      detailSection('Local signals', data.local_rationale_html),
      detailSection(data.source_label || 'Source description', data.source_html, 'review-source-text'),
    ].filter(Boolean).join('') || '<p class="muted">No additional details are stored for this item.</p>';
  }

  function renderItem(item) {
    const open = Boolean(state.openItems[item.id]);
    const tags = (item.tags || []).map(tag => `<span class="review-tag">${escapeHTML(tag)}</span>`).join('');
    const readPending = Boolean(state.readMutations[item.id]);
    const resource = safeHref(item.url);
    const title = resource
      ? `<a class="review-item-title" href="${escapeHTML(resource)}" target="_blank" rel="noopener noreferrer" data-action="open-item-link" data-item-id="${item.id}">${escapeHTML(item.title)}</a>`
      : `<span class="review-item-title">${escapeHTML(item.title)}</span>`;
    const score = scoreLabel(item);
    return `<article class="review-item${item.is_read ? ' is-read' : ''}" data-item-id="${item.id}">
      <div class="review-item-main">
        ${score ? `<span class="review-score review-score-${escapeHTML(item.ai_state)}">${escapeHTML(score)}</span>` : ''}
        ${title}
        <div class="review-item-actions">
          <button type="button" data-action="star" data-item-id="${item.id}" aria-label="${item.is_starred ? 'Remove favorite' : 'Add favorite'}" class="review-icon-button${item.is_starred ? ' active' : ''}">★</button>
          <button type="button" data-action="read-later" data-item-id="${item.id}" aria-label="${item.is_read_later ? 'Remove from Read later' : 'Add to Read later'}" class="review-icon-button${item.is_read_later ? ' active' : ''}">↗</button>
          <button type="button" data-action="read" data-item-id="${item.id}" aria-label="${item.is_read ? 'Mark as unread' : 'Mark as read'}" class="review-icon-button review-read-button" ${readPending ? 'disabled aria-busy="true"' : ''}>${item.is_read ? '○' : '●'}</button>
        </div>
        <div class="review-item-meta">
          <span>${escapeHTML(item.feed_title)}</span>${item.author ? `<span>${escapeHTML(item.author)}</span>` : ''}${item.published_at ? `<time datetime="${escapeHTML(item.published_at)}">${escapeHTML(formatTimestamp(item.published_at))} UTC</time>` : ''}
          ${item.local_score !== null && item.local_score !== undefined ? `<span>Local ${escapeHTML(item.local_score)}</span>` : ''}
        </div>
        <div class="review-item-secondary"><span class="review-tags">${tags}</span><button class="review-content-toggle" type="button" data-action="toggle-item" data-item-id="${item.id}" aria-expanded="${open}" aria-controls="review-item-details-${item.id}"><span>${escapeHTML(State.contentToggleLabel(open))}</span><span class="review-item-caret" aria-hidden="true">›</span></button></div>
      </div>
      <div id="review-item-details-${item.id}" class="review-item-details" ${open ? '' : 'hidden'}>${open ? renderItemDetails(item.id) : ''}</div>
    </article>`;
  }

  function renderDayItems(day, container) {
    const data = state.dayData[day.day];
    if (!data) {
      container.innerHTML = state.displayMode === 'direct'
        ? '<div class="review-loading review-deferred">Items load automatically as this day approaches.</div>'
        : '<div class="review-loading">Loading items…</div>';
      return;
    }
    if (data.loading && !data.items.length) {
      container.innerHTML = '<div class="review-loading">Loading items…</div>';
      return;
    }
    if (data.error) {
      container.innerHTML = `<div class="review-error">${escapeHTML(data.error)} <button type="button" data-action="retry-day" data-day="${escapeHTML(day.day)}">Retry</button></div>`;
      return;
    }
    if (!data.items.length) {
      container.innerHTML = '<p class="review-empty">No items match the current filters for this day.</p>';
      return;
    }
    let currentCohort = '';
    const fragments = [];
    data.items.forEach(item => {
      const label = cohortLabel(item);
      if (state.filters.sort === 'ai' && label !== currentCohort) {
        currentCohort = label;
        fragments.push(`<h3 class="review-cohort-heading">${escapeHTML(label)}</h3>`);
      }
      fragments.push(renderItem(item));
    });
    if (data.hasMore) fragments.push(`<button class="review-load-more" type="button" data-action="load-more" data-day="${escapeHTML(day.day)}" ${data.loading ? 'disabled' : ''}>${data.loading ? 'Loading…' : `Show next ${state.filters.page_size}`}</button>`);
    container.innerHTML = fragments.join('');
  }

  function daySummaryText(day) {
    const base = State.countSummary(day, state.filters, options);
    const parts = [base];
    if (day.scored) parts.push(`${day.scored.toLocaleString()} AI-ranked`);
    return parts.filter(Boolean).join(' · ');
  }

  function cancelDirectLoadSchedule() {
    if (!directLoadFrame) return;
    cancelAnimationFrame(directLoadFrame);
    directLoadFrame = 0;
  }

  function scheduleDirectLoads() {
    if (directLoadFrame || state.displayMode !== 'direct' || !daysRoot) return;
    const generation = state.generation;
    directLoadFrame = requestAnimationFrame(() => {
      directLoadFrame = 0;
      if (state.generation !== generation || state.displayMode !== 'direct' || !daysRoot) return;
      let capacity = Math.max(0, DIRECT_LOAD_CONCURRENCY - dayControllers.size);
      if (!capacity) return;
      const rootRect = daysRoot.getBoundingClientRect();
      const lowerBound = rootRect.top - DIRECT_LOAD_MARGIN;
      const upperBound = rootRect.bottom + DIRECT_LOAD_MARGIN;
      for (const details of daysRoot.querySelectorAll('details.review-day[open]')) {
        if (!capacity) break;
        const day = details.dataset.day;
        if (!day || !state.openDays[day] || state.dayData[day]) continue;
        const rectangle = details.getBoundingClientRect();
        if (rectangle.bottom < lowerBound || rectangle.top > upperBound) continue;
        capacity -= 1;
        loadDay(day, false);
      }
    });
  }

  function renderDays() {
    if (!daysRoot) return;
    cancelDirectLoadSchedule();
    if (state.status === 'loading' && !state.days.length) {
      daysRoot.innerHTML = '<div class="review-loading review-loading-page">Loading the review stream…</div>';
      return;
    }
    if (state.status === 'error') {
      daysRoot.innerHTML = `<div class="review-error review-error-page"><strong>The review stream could not be loaded.</strong><span>${escapeHTML(state.error)}</span><button type="button" data-action="retry-days">Retry</button></div>`;
      return;
    }
    if (!state.days.length) {
      daysRoot.innerHTML = '<div class="review-empty review-empty-page"><strong>No items match these filters.</strong><span>Change a filter or select Everything.</span></div>';
      return;
    }
    daysRoot.innerHTML = '';
    const immediateLoads = [];
    const renderGeneration = state.generation;
    state.days.forEach(day => {
      const details = document.createElement('details');
      details.className = `review-day${day.complete ? ' is-complete' : ''}`;
      details.dataset.day = day.day;
      details.open = Boolean(state.openDays[day.day]);
      const hiddenCount = Math.max(0, day.total - day.matching);
      const brief = day.brief;
      details.innerHTML = `<summary class="review-day-summary">
        <span class="review-day-title"><strong>${escapeHTML(formatDay(day.day))}</strong><span>${escapeHTML(daySummaryText(day))}</span></span>
        <span class="review-day-state">${day.complete ? 'Complete' : 'To review'}</span>
      </summary>
      <div class="review-day-body">
        ${brief ? `<details class="review-daily-brief"><summary>Daily brief · ${Number(brief.selected_count || 0).toLocaleString()} selected</summary><div class="review-daily-brief-body review-markdown">${brief.html || ''}</div><footer>${escapeHTML(brief.model || '')}${brief.completed_at ? ` · ${escapeHTML(brief.completed_at)}` : ''}</footer></details>` : '<p class="review-no-brief">No daily brief is stored for this day.</p>'}
        <div class="review-day-actions">
          <button type="button" data-action="mark-visible-read" data-day="${escapeHTML(day.day)}">Mark loaded items read</button>
          <button type="button" data-action="mark-day-read" data-day="${escapeHTML(day.day)}" data-total="${day.total}" data-unread="${day.unread}" data-hidden="${hiddenCount}" ${(state.finishingDays[day.day] || !day.unread) ? 'disabled' : ''}>${escapeHTML(State.dayReadActionLabel(day.day, today, day.unread, Boolean(state.finishingDays[day.day])))}</button>
        </div>
        <div class="review-day-items" data-day-items="${escapeHTML(day.day)}"></div>
      </div>`;
      details.dataset.openState = String(details.open);
      details.addEventListener('toggle', () => {
        const open = details.open;
        if (details.dataset.openState === String(open)) return;
        details.dataset.openState = String(open);
        transition({ type: 'TOGGLE_DAY', day: day.day, open }, false);
        persistOpenDays();
        if (open && !state.dayData[day.day]) loadDay(day.day, false);
      });
      daysRoot.appendChild(details);
      const itemContainer = details.querySelector('[data-day-items]');
      if (details.open) {
        renderDayItems(day, itemContainer);
        if (!state.dayData[day.day]) {
          if (state.displayMode !== 'direct') immediateLoads.push(day.day);
        }
      }
    });
    immediateLoads.forEach(day => queueMicrotask(() => {
      if (state.generation === renderGeneration && state.openDays[day]
          && !state.dayData[day]) loadDay(day, false);
    }));
    scheduleDirectLoads();
  }

  function renderReview() {
    syncControls();
    if (resultStatus) {
      const counts = state.counts || {};
      resultStatus.textContent = state.status === 'loading' && !state.days.length
        ? 'Loading…'
        : State.countSummary(counts, state.filters, options);
    }
    renderDays();
  }

  function refreshFilters(values, preset = null) {
    clearTimeout(searchTimer); searchTimer = null;
    if (preset) transition({ type: 'APPLY_PRESET', preset, options });
    else transition({ type: 'SET_FILTERS', values, options });
    syncURL();
    abortScopedRequests();
    loadDays();
  }

  function abortScopedRequests() {
    cancelDirectLoadSchedule();
    dayControllers.forEach(controller => controller.abort()); dayControllers.clear();
    itemControllers.forEach(controller => controller.abort()); itemControllers.clear();
  }

  function refreshFilter(field, value) {
    transition({ type: 'SET_FILTER', field, value, options });
    syncURL(); abortScopedRequests(); loadDays();
  }

  async function loadDays() {
    if (!scope.groupId && !scope.feedId) {
      state = { ...state, status: 'ready', days: [], counts: {} };
      renderReview(); return;
    }
    if (daysController) daysController.abort();
    daysController = new AbortController();
    const generation = state.generation;
    const requestId = nextRequestId('days');
    transition({ type: 'REQUEST_DAYS', generation, requestId });
    const query = filtersParams(); scopeParams(query);
    try {
      const payload = await requestJSON(`/api/review/days?${query}`, { signal: daysController.signal });
      transition({ type: 'RECEIVE_DAYS', generation, requestId, payload });
      persistOpenDays();
    } catch (error) {
      if (error.name === 'AbortError') return;
      if (state.filters.source && /outside this review scope/i.test(error.message)) {
        refreshFilters({ ...state.filters, source: 0 }); return;
      }
      transition({ type: 'FAIL_DAYS', generation, requestId, error: error.message });
    }
  }

  async function loadDay(day, append) {
    const generation = state.generation;
    const existing = state.dayData[day];
    if (existing?.loading) return;
    const previousController = dayControllers.get(day); if (previousController) previousController.abort();
    const controller = new AbortController(); dayControllers.set(day, controller);
    const requestId = nextRequestId(`day-${day}`);
    transition({ type: 'REQUEST_DAY', generation, day, append, requestId });
    const query = filtersParams(); scopeParams(query);
    if (append && existing?.nextCursor) query.set('cursor', existing.nextCursor);
    try {
      const payload = await requestJSON(`/api/review/days/${encodeURIComponent(day)}/items?${query}`, { signal: controller.signal });
      transition({ type: 'RECEIVE_DAY', generation, day, requestId, payload, append });
    } catch (error) {
      if (error.name === 'AbortError') return;
      transition({ type: 'FAIL_DAY', generation, day, requestId, error: error.message });
    } finally {
      if (dayControllers.get(day) === controller) dayControllers.delete(day);
      scheduleDirectLoads();
    }
  }

  async function loadItem(itemId) {
    const existing = state.itemDetails[itemId];
    if (existing?.status === 'loading' || existing?.status === 'ready') return;
    const controller = new AbortController(); itemControllers.set(itemId, controller);
    const requestId = nextRequestId(`item-${itemId}`);
    transition({ type: 'REQUEST_ITEM', itemId, requestId });
    const query = new URLSearchParams(); scopeParams(query);
    try {
      const payload = await requestJSON(`/api/review/items/${itemId}?${query}`, { signal: controller.signal });
      transition({ type: 'RECEIVE_ITEM', itemId, requestId, payload });
    } catch (error) {
      if (error.name === 'AbortError') return;
      transition({ type: 'FAIL_ITEM', itemId, requestId, error: error.message });
    } finally {
      if (itemControllers.get(itemId) === controller) itemControllers.delete(itemId);
    }
  }

  function authoritativeReload() {
    transition({ type: 'SET_FILTERS', values: state.filters, options });
    syncURL(); abortScopedRequests(); loadDays();
  }

  async function changeRead(itemId) {
    if (state.readMutations[itemId]) return;
    const item = Object.values(state.dayData).flatMap(data => data.items).find(candidate => Number(candidate.id) === Number(itemId));
    if (!item) return;
    const read = !item.is_read;
    const token = `${Date.now()}-${Math.random()}`;
    transition({ type: 'OPTIMISTIC_READ', itemId, read, token });
    try {
      await requestJSON(`/api/items/${itemId}/read`, { method: 'POST', body: JSON.stringify({ read }) });
      transition({ type: 'CONFIRM_READ', itemId, token });
      authoritativeReload();
    } catch (error) {
      transition({ type: 'ROLLBACK_READ', itemId, token });
      notify(error.message);
    }
  }

  async function changeBoolean(itemId, endpoint, field, value, successMessage) {
    try {
      await requestJSON(`/api/items/${itemId}/${endpoint}`, { method: 'POST', body: JSON.stringify({ [field]: value }) });
      notify(successMessage); authoritativeReload();
    } catch (error) { notify(error.message); }
  }

  async function markVisibleRead(day) {
    const items = (state.dayData[day]?.items || []).filter(item => !item.is_read);
    if (!items.length) { notify('Every loaded item is already read'); return; }
    try {
      const result = await requestJSON('/api/items/bulk-read', {
        method: 'POST', body: JSON.stringify({ mode: 'selected', read: true, item_ids: items.map(item => item.id) }),
      });
      notify(`${result.changed} loaded item${result.changed === 1 ? '' : 's'} marked read`); authoritativeReload();
    } catch (error) { notify(error.message); }
  }

  async function markDayRead(day, total, unread, hidden) {
    if (state.finishingDays[day] || unread <= 0) return;
    const dayName = day === today ? 'today' : formatDay(day);
    const scopeName = scope.title ? ` in ${scope.title}` : '';
    const itemWord = unread === 1 ? 'item' : 'items';
    const hiddenNote = hidden
      ? `\n\n${hidden.toLocaleString()} of the ${total.toLocaleString()} items are outside the current filters.`
      : '';
    const message = `Mark ${dayName} as read${scopeName}?\n\nThis marks ${unread.toLocaleString()} unread ${itemWord} from this day as read.${hiddenNote}`;
    if (!window.confirm(message)) return;
    transition({ type: 'FINISH_DAY_START', day });
    const body = {}; if (scope.feedId) body.feed_id = scope.feedId; else body.group_id = scope.groupId;
    try {
      const result = await requestJSON(`/api/review/days/${encodeURIComponent(day)}/finish`, { method: 'POST', body: JSON.stringify(body) });
      transition({ type: 'FINISH_DAY_SUCCESS', day, payload: result });
      persistOpenDays(); notify(`${result.changed} item${result.changed === 1 ? '' : 's'} marked read`);
      authoritativeReload();
    } catch (error) {
      transition({ type: 'FINISH_DAY_FAIL', day, error: error.message }); notify(error.message);
    }
  }

  async function saveDisplayMode(mode) {
    const normalized = State.normalizeDisplayMode(mode);
    if (!scope.preferenceGroupId || normalized === state.displayMode) {
      if (displayControl) displayControl.value = state.displayMode;
      return;
    }
    if (displayControl) displayControl.disabled = true;
    displayControlLabel?.classList.add('is-saving');
    try {
      const result = await requestJSON(`/api/groups/${scope.preferenceGroupId}`, {
        method: 'PATCH', body: JSON.stringify({ review_display_mode: normalized }),
      });
      const appliedMode = State.normalizeDisplayMode(result.review_display_mode || normalized);
      options.displayMode = appliedMode;
      options.defaultPreset = isArxivScope
        ? (appliedMode === 'direct' ? 'catch-up' : 'best-unread')
        : 'everything';
      transition({ type: 'SET_DISPLAY_MODE', mode: appliedMode });
      persistOpenDays();
      notify(appliedMode === 'direct'
        ? 'Direct item list saved for this group'
        : 'Focused daily review saved for this group');
      // Display density and semantic filtering are independent choices.
      // Changing the day layout must never silently hide or reveal items.
      refreshFilters(state.filters);
    } catch (error) {
      if (displayControl) displayControl.value = state.displayMode;
      notify(error.message);
    } finally {
      if (displayControl) displayControl.disabled = false;
      displayControlLabel?.classList.remove('is-saving');
    }
  }

  daysRoot?.addEventListener('scroll', scheduleDirectLoads, { passive: true });
  window.addEventListener('resize', scheduleDirectLoads);

  displayControl?.addEventListener('change', () => saveDisplayMode(displayControl.value));
  presetControl?.addEventListener('change', () => refreshFilters(null, presetControl.value));
  searchControl?.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      refreshFilter('q', searchControl.value);
    }, 250);
  });
  [
    [minAIControl, 'min_ai'], [sortControl, 'sort'], [pageSizeControl, 'page_size'],
    [readControl, 'read'], [savedControl, 'saved'], [aiControl, 'ai'],
    [decisionControl, 'decision'], [sourceControl, 'source'], [fromControl, 'from'], [toControl, 'to'],
  ].forEach(([control, field]) => control?.addEventListener('change', () => {
    refreshFilter(field, control.value);
  }));
  document.getElementById('review-reset')?.addEventListener('click', () => {
    refreshFilters(null, State.defaultPreset({ ...options, displayMode: state.displayMode }));
  });

  daysRoot?.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    const day = button.dataset.day;
    const itemId = Number(button.dataset.itemId || 0);
    if (action === 'retry-days') loadDays();
    else if (action === 'retry-day') loadDay(day, false);
    else if (action === 'load-more') loadDay(day, true);
    else if (action === 'toggle-item') {
      const open = !state.openItems[itemId];
      transition({ type: 'TOGGLE_ITEM', itemId, open });
      if (open) loadItem(itemId);
    } else if (action === 'read') changeRead(itemId);
    else if (action === 'star') {
      const item = Object.values(state.dayData).flatMap(data => data.items).find(candidate => Number(candidate.id) === itemId);
      if (item) changeBoolean(itemId, 'star', 'starred', !item.is_starred, item.is_starred ? 'Removed from Favorites' : 'Added to Favorites');
    } else if (action === 'read-later') {
      const item = Object.values(state.dayData).flatMap(data => data.items).find(candidate => Number(candidate.id) === itemId);
      if (item) changeBoolean(itemId, 'read-later', 'read_later', !item.is_read_later, item.is_read_later ? 'Removed from Read later' : 'Added to Read later');
    } else if (action === 'mark-visible-read') markVisibleRead(day);
    else if (action === 'mark-day-read') markDayRead(day, Number(button.dataset.total || 0), Number(button.dataset.unread || 0), Number(button.dataset.hidden || 0));
    else if (action === 'open-item-link') {
      const item = Object.values(state.dayData).flatMap(data => data.items).find(candidate => Number(candidate.id) === itemId);
      if (item && !item.is_read) window.setTimeout(() => changeRead(itemId), 0);
    }
  });

  renderReview();
  loadDays();
})();
