(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DistillFeedReviewState = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const FILTER_FIELDS = Object.freeze([
    'q', 'read', 'saved', 'ai', 'decision', 'min_ai', 'source', 'from', 'to', 'sort', 'page_size',
  ]);
  const PRESETS = new Set(['best-unread', 'catch-up', 'today', 'awaiting-ai', 'starred', 'everything', 'custom']);
  const READ = new Set(['all', 'unread', 'read']);
  const SAVED = new Set(['all', 'starred', 'read-later']);
  const AI = new Set(['all', 'scored', 'pending', 'not-sent']);
  const DECISION = new Set(['all', 'keep', 'drop']);
  const SORT = new Set(['ai', 'date', 'local']);
  const PAGE = new Set([10, 25, 50]);
  const DISPLAY = new Set(['daily', 'direct']);

  function clampInteger(value, fallback, minimum, maximum) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(minimum, Math.min(maximum, parsed));
  }

  function validDay(value) {
    const text = String(value || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return '';
    const date = new Date(`${text}T00:00:00Z`);
    return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === text ? text : '';
  }

  function defaultPreset(options = {}) {
    const configured = String(options.defaultPreset || '').trim();
    if (PRESETS.has(configured) && configured !== 'custom') return configured;
    if (!options.isArxiv) return 'everything';
    return normalizeDisplayMode(options.displayMode) === 'direct' ? 'catch-up' : 'best-unread';
  }

  function defaultSort(options = {}) {
    const configured = String(options.defaultSort || '').trim();
    if (SORT.has(configured)) return configured;
    return options.isArxiv ? 'ai' : 'date';
  }

  function presetDefaults(preset, options = {}) {
    const today = validDay(options.today) || new Date().toISOString().slice(0, 10);
    const defaultMinAI = clampInteger(options.defaultMinAI, 70, 0, 100);
    const base = {
      preset, q: '', read: 'all', saved: 'all', ai: 'all', decision: 'all',
      min_ai: 0, source: 0, from: '', to: '', sort: defaultSort(options), page_size: 25,
    };
    if (preset === 'best-unread') Object.assign(base, { read: 'unread', ai: 'scored', decision: 'keep', min_ai: defaultMinAI });
    else if (preset === 'catch-up') Object.assign(base, { read: 'unread' });
    else if (preset === 'today') Object.assign(base, { from: today, to: today });
    else if (preset === 'awaiting-ai') Object.assign(base, { ai: 'pending', sort: 'local' });
    else if (preset === 'starred') Object.assign(base, { saved: 'starred' });
    return base;
  }

  function normalizeFilters(input = {}, options = {}) {
    const preset = PRESETS.has(String(input.preset || '')) ? String(input.preset) : defaultPreset(options);
    const defaults = presetDefaults(preset, options);
    const value = { ...defaults, ...input, preset };
    const page = clampInteger(value.page_size, 25, 10, 50);
    const normalized = {
      preset,
      q: String(value.q || '').trim().slice(0, 200),
      read: READ.has(value.read) ? value.read : defaults.read,
      saved: SAVED.has(value.saved) ? value.saved : defaults.saved,
      ai: AI.has(value.ai) ? value.ai : defaults.ai,
      decision: DECISION.has(value.decision) ? value.decision : defaults.decision,
      min_ai: clampInteger(value.min_ai, defaults.min_ai, 0, 100),
      source: clampInteger(value.source, 0, 0, 2147483647),
      from: validDay(value.from),
      to: validDay(value.to),
      sort: SORT.has(value.sort) ? value.sort : defaults.sort,
      page_size: PAGE.has(page) ? page : 25,
    };
    if (normalized.ai === 'pending' || normalized.ai === 'not-sent') {
      normalized.min_ai = 0;
      normalized.decision = 'all';
    }
    return normalized;
  }

  function hasActiveFilters(filters = {}, options = {}) {
    const normalized = normalizeFilters(filters, options);
    return Boolean(
      normalized.q || normalized.read !== 'all' || normalized.saved !== 'all'
      || normalized.ai !== 'all' || normalized.decision !== 'all'
      || normalized.min_ai || normalized.source || normalized.from || normalized.to
    );
  }

  function plural(count, singular, pluralValue = `${singular}s`) {
    return `${count.toLocaleString()} ${count === 1 ? singular : pluralValue}`;
  }

  function countSummary(counts = {}, filters = {}, options = {}) {
    const total = Number(counts.total || 0);
    const matching = Number(counts.matching || 0);
    const unread = Number(counts.unread || 0);
    const parts = hasActiveFilters(filters, options)
      ? [plural(matching, 'result'), `${total.toLocaleString()} total`, `${unread.toLocaleString()} unread overall`]
      : [plural(total, 'item'), `${unread.toLocaleString()} unread`];
    const pending = Number(counts.pending || 0);
    if (pending) parts.push(`${pending.toLocaleString()} awaiting AI`);
    return parts.join(' · ');
  }

  function contentToggleLabel(open = false) {
    return open ? 'Hide content' : 'View content';
  }

  function dayReadActionLabel(day, today, unread, finishing = false) {
    if (finishing) return 'Marking as read…';
    const isToday = String(day || '') === String(today || '');
    if (Number(unread || 0) <= 0) return isToday ? 'Today is read' : 'Day is read';
    return isToday ? 'Mark today as read' : 'Mark day as read';
  }

  function normalizeDisplayMode(value) {
    const mode = String(value || '').trim().toLowerCase();
    return DISPLAY.has(mode) ? mode : 'daily';
  }

  function initialState(filters = {}, options = {}) {
    return {
      filters: normalizeFilters(filters, options), generation: 0,
      status: 'idle', error: '', counts: {}, sources: [], days: [],
      defaultOpenDay: '', displayMode: normalizeDisplayMode(options.displayMode),
      openDays: {}, dayData: {}, openItems: {}, itemDetails: {},
      readMutations: {}, finishingDays: {}, activeDaysRequestId: '',
    };
  }

  function resetForFilters(state, filters) {
    return {
      ...state, filters, generation: state.generation + 1, status: 'loading', error: '',
      counts: {}, days: [], defaultOpenDay: '', dayData: {}, openItems: {}, itemDetails: {},
    };
  }

  function replaceItem(state, itemId, updater) {
    const dayData = {};
    let changed = false;
    Object.entries(state.dayData).forEach(([day, data]) => {
      const items = data.items.map(item => {
        if (Number(item.id) !== Number(itemId)) return item;
        changed = true;
        return updater(item);
      });
      dayData[day] = items === data.items ? data : { ...data, items };
    });
    return changed ? { ...state, dayData } : state;
  }

  function reducer(state, action) {
    switch (action.type) {
      case 'APPLY_PRESET': {
        const filters = normalizeFilters(presetDefaults(action.preset, action.options), action.options);
        return resetForFilters(state, filters);
      }
      case 'SET_FILTER': {
        if (!FILTER_FIELDS.includes(action.field)) return state;
        const values = { ...state.filters, [action.field]: action.value, preset: 'custom' };
        if (action.field === 'ai' && ['pending', 'not-sent'].includes(String(action.value))) {
          values.min_ai = 0;
          values.decision = 'all';
        } else if (action.field === 'min_ai' && Number.parseInt(action.value, 10) > 0) {
          values.ai = 'scored';
        } else if (action.field === 'decision' && ['keep', 'drop'].includes(String(action.value))) {
          values.ai = 'scored';
        }
        const filters = normalizeFilters(values, action.options);
        return resetForFilters(state, filters);
      }
      case 'SET_FILTERS': {
        const filters = normalizeFilters({ ...state.filters, ...action.values }, action.options);
        return resetForFilters(state, filters);
      }
      case 'REQUEST_DAYS':
        return action.generation === state.generation
          ? { ...state, status: 'loading', error: '', activeDaysRequestId: String(action.requestId || '') }
          : state;
      case 'RECEIVE_DAYS': {
        if (action.generation !== state.generation || String(action.requestId || '') !== state.activeDaysRequestId) return state;
        const payload = action.payload || {};
        const days = payload.days || [];
        const availableDays = new Set(days.map(day => String(day.day || '')));
        const serverMode = normalizeDisplayMode(payload.scope?.review_display_mode || state.displayMode);
        const modeChanged = serverMode !== state.displayMode;
        const openDays = modeChanged ? {} : Object.fromEntries(
          Object.entries(state.openDays)
            .filter(([day, open]) => availableDays.has(day) && typeof open === 'boolean'),
        );
        if (serverMode === 'direct') {
          days.forEach(day => {
            const key = String(day.day || '');
            if (key && !Object.prototype.hasOwnProperty.call(openDays, key)) openDays[key] = true;
          });
        } else if (!Object.keys(openDays).length && payload.default_open_day) {
          openDays[payload.default_open_day] = true;
        }
        return {
          ...state, status: 'ready', error: '', activeDaysRequestId: '', counts: payload.counts || {},
          sources: payload.sources || [], days,
          defaultOpenDay: payload.default_open_day || '', displayMode: serverMode, openDays,
        };
      }
      case 'FAIL_DAYS':
        return action.generation === state.generation && String(action.requestId || '') === state.activeDaysRequestId
          ? { ...state, status: 'error', error: action.error || 'Review could not be loaded', activeDaysRequestId: '' }
          : state;
      case 'SET_DISPLAY_MODE': {
        const displayMode = normalizeDisplayMode(action.mode);
        if (displayMode === state.displayMode) return state;
        const openDays = {};
        if (displayMode === 'direct') {
          state.days.forEach(day => { openDays[day.day] = true; });
        } else if (state.defaultOpenDay) {
          state.days.forEach(day => { openDays[day.day] = day.day === state.defaultOpenDay; });
        }
        return { ...state, displayMode, openDays };
      }
      case 'TOGGLE_DAY':
        return { ...state, openDays: { ...state.openDays, [action.day]: Boolean(action.open) } };
      case 'REQUEST_DAY': {
        if (action.generation !== state.generation) return state;
        const previous = state.dayData[action.day] || { items: [], nextCursor: '', hasMore: false };
        return {
          ...state,
          dayData: { ...state.dayData, [action.day]: {
            ...previous, loading: true, error: '', requestGeneration: action.generation,
            requestId: String(action.requestId || ''), append: Boolean(action.append),
          } },
        };
      }
      case 'RECEIVE_DAY': {
        if (action.generation !== state.generation) return state;
        const previous = state.dayData[action.day] || { items: [] };
        if (previous.requestGeneration !== action.generation || previous.requestId !== String(action.requestId || '')) return state;
        const incoming = action.payload?.items || [];
        const combined = action.append ? [...previous.items, ...incoming] : incoming;
        const seen = new Set();
        const items = combined.filter(item => {
          const id = Number(item.id);
          if (seen.has(id)) return false;
          seen.add(id); return true;
        });
        return {
          ...state,
          dayData: {
            ...state.dayData,
            [action.day]: {
              items, nextCursor: action.payload?.next_cursor || '', hasMore: Boolean(action.payload?.has_more),
              total: Number(action.payload?.total || items.length), loading: false, error: '',
              requestGeneration: action.generation, requestId: '',
            },
          },
        };
      }
      case 'FAIL_DAY': {
        if (action.generation !== state.generation) return state;
        const previous = state.dayData[action.day] || { items: [] };
        if (previous.requestId !== String(action.requestId || '')) return state;
        return { ...state, dayData: { ...state.dayData, [action.day]: { ...previous, loading: false, requestId: '', error: action.error || 'Items could not be loaded' } } };
      }
      case 'TOGGLE_ITEM':
        return { ...state, openItems: { ...state.openItems, [action.itemId]: Boolean(action.open) } };
      case 'REQUEST_ITEM':
        return { ...state, itemDetails: { ...state.itemDetails, [action.itemId]: { status: 'loading', data: null, error: '', requestId: String(action.requestId || '') } } };
      case 'RECEIVE_ITEM': {
        const previous = state.itemDetails[action.itemId];
        if (!previous || previous.requestId !== String(action.requestId || '')) return state;
        return { ...state, itemDetails: { ...state.itemDetails, [action.itemId]: { status: 'ready', data: action.payload, error: '', requestId: '' } } };
      }
      case 'FAIL_ITEM': {
        const previous = state.itemDetails[action.itemId];
        if (!previous || previous.requestId !== String(action.requestId || '')) return state;
        return { ...state, itemDetails: { ...state.itemDetails, [action.itemId]: { status: 'error', data: null, error: action.error || 'Details could not be loaded', requestId: '' } } };
      }
      case 'OPTIMISTIC_READ': {
        if (state.readMutations[action.itemId]) return state;
        const current = Object.values(state.dayData).flatMap(data => data.items).find(item => Number(item.id) === Number(action.itemId));
        if (!current) return state;
        const next = replaceItem(state, action.itemId, item => ({ ...item, is_read: Boolean(action.read) }));
        return { ...next, readMutations: { ...state.readMutations, [action.itemId]: { token: action.token, previous: Boolean(current.is_read) } } };
      }
      case 'CONFIRM_READ': {
        const mutation = state.readMutations[action.itemId];
        if (!mutation || mutation.token !== action.token) return state;
        const readMutations = { ...state.readMutations }; delete readMutations[action.itemId];
        return { ...state, readMutations };
      }
      case 'ROLLBACK_READ': {
        const mutation = state.readMutations[action.itemId];
        if (!mutation || mutation.token !== action.token) return state;
        const restored = replaceItem(state, action.itemId, item => ({ ...item, is_read: mutation.previous }));
        const readMutations = { ...restored.readMutations }; delete readMutations[action.itemId];
        return { ...restored, readMutations };
      }
      case 'FINISH_DAY_START':
        if (state.finishingDays[action.day]) return state;
        return { ...state, finishingDays: { ...state.finishingDays, [action.day]: true } };
      case 'FINISH_DAY_SUCCESS': {
        const days = state.days.map(day => day.day === action.day ? { ...day, unread: 0, complete: true } : day);
        const data = state.dayData[action.day];
        const dayData = data ? { ...state.dayData, [action.day]: { ...data, items: data.items.map(item => ({ ...item, is_read: true })) } } : state.dayData;
        const finishingDays = { ...state.finishingDays }; delete finishingDays[action.day];
        return { ...state, days, dayData, finishingDays, openDays: { ...state.openDays, [action.day]: false } };
      }
      case 'FINISH_DAY_FAIL': {
        const finishingDays = { ...state.finishingDays }; delete finishingDays[action.day];
        return { ...state, finishingDays, error: action.error || 'The day could not be marked as read' };
      }
      default:
        return state;
    }
  }

  return {
    FILTER_FIELDS, presetDefaults, defaultPreset, defaultSort, normalizeFilters,
    hasActiveFilters, countSummary, contentToggleLabel, dayReadActionLabel,
    normalizeDisplayMode, initialState, reducer,
  };
}));
