'use strict';

const assert = require('node:assert/strict');
const Review = require('../rss_reader/static/review-state.js');

const options = { defaultMinAI: 80, today: '2026-08-18', isArxiv: true, defaultPreset: 'best-unread', defaultSort: 'ai', displayMode: 'daily' };
const ordinaryOptions = { defaultMinAI: 80, today: '2026-08-18', isArxiv: false, defaultPreset: 'everything', defaultSort: 'date', displayMode: 'direct' };

function reduce(state, action) { return Review.reducer(state, action); }

// Every preset has a stable, explicit contract.
const expectedPresets = {
  'best-unread': { read: 'unread', ai: 'scored', decision: 'keep', min_ai: 80, sort: 'ai' },
  'catch-up': { read: 'unread', ai: 'all', decision: 'all', min_ai: 0, sort: 'ai' },
  'today': { from: '2026-08-18', to: '2026-08-18' },
  'awaiting-ai': { ai: 'pending', sort: 'local' },
  'starred': { saved: 'starred' },
  'everything': { read: 'all', saved: 'all', ai: 'all', decision: 'all', min_ai: 0 },
};
for (const [preset, expected] of Object.entries(expectedPresets)) {
  const actual = Review.normalizeFilters({ preset }, options);
  for (const [key, value] of Object.entries(expected)) assert.equal(actual[key], value, `${preset}.${key}`);
}

// Ordinary OPML groups and RSS feeds are inboxes: no-query and invalid-query
// states show every item chronologically, independent of the day layout.
const ordinaryDefault = Review.normalizeFilters({}, ordinaryOptions);
assert.equal(ordinaryDefault.preset, 'everything');
assert.equal(ordinaryDefault.sort, 'date');
assert.equal(ordinaryDefault.read, 'all');
assert.equal(Review.defaultPreset({ ...ordinaryOptions, displayMode: 'daily' }), 'everything');
assert.equal(Review.defaultPreset({ ...ordinaryOptions, displayMode: 'direct' }), 'everything');
assert.equal(Review.normalizeFilters({ preset: 'invalid' }, ordinaryOptions).preset, 'everything');

// Item expansion is an explicit state transition, independent of the external title link.
assert.equal(Review.contentToggleLabel(false), 'View content');
assert.equal(Review.contentToggleLabel(true), 'Hide content');

// Unfiltered counts describe the item set directly; filtered counts describe results.
assert.equal(
  Review.countSummary({ total: 18, matching: 18, unread: 10, pending: 0 }, ordinaryDefault, ordinaryOptions),
  '18 items · 10 unread',
);
const searched = Review.normalizeFilters({ preset: 'custom', q: 'logic' }, ordinaryOptions);
assert.equal(
  Review.countSummary({ total: 18, matching: 4, unread: 10, pending: 0 }, searched, ordinaryOptions),
  '4 results · 18 total · 10 unread overall',
);
assert.equal(Review.dayReadActionLabel('2026-08-18', '2026-08-18', 7), 'Mark today as read');
assert.equal(Review.dayReadActionLabel('2026-08-17', '2026-08-18', 7), 'Mark day as read');
assert.equal(Review.dayReadActionLabel('2026-08-18', '2026-08-18', 0), 'Today is read');
assert.equal(Review.dayReadActionLabel('2026-08-17', '2026-08-18', 0), 'Day is read');
assert.equal(Review.dayReadActionLabel('2026-08-17', '2026-08-18', 7, true), 'Marking as read…');

// Every filter accepted by the toolbar changes state, selects Custom, and starts a new query generation.
const values = {
  q: 'causal graph', read: 'read', saved: 'read-later', ai: 'not-sent',
  decision: 'drop', min_ai: '90', source: '12', from: '2026-08-01',
  to: '2026-08-18', sort: 'date', page_size: '50',
};
let state = Review.initialState({ preset: 'everything' }, options);
for (const field of Review.FILTER_FIELDS) {
  const generation = state.generation;
  state = reduce(state, { type: 'SET_FILTER', field, value: values[field], options });
  assert.equal(state.generation, generation + 1, `${field} generation`);
  assert.equal(state.filters.preset, 'custom', `${field} preset`);
  assert.deepEqual(String(state.filters[field]), String(Review.normalizeFilters({ ...state.filters, [field]: values[field] }, options)[field]));
}

// Invalid filter values are normalized rather than leaking inconsistent states into the URL/API.
const normalized = Review.normalizeFilters({
  preset: 'invalid', read: 'maybe', saved: 'later-ish', ai: 'unknown', decision: 'maybe',
  min_ai: 900, source: -4, from: 'not-a-day', to: '2026-99-99', sort: 'random', page_size: 37,
}, options);
assert.equal(normalized.preset, 'best-unread');
assert.equal(normalized.read, 'unread');
assert.equal(normalized.saved, 'all');
assert.equal(normalized.ai, 'scored');
assert.equal(normalized.decision, 'keep');
assert.equal(normalized.min_ai, 100);
assert.equal(normalized.source, 0);
assert.equal(normalized.from, '');
assert.equal(normalized.to, '');
assert.equal(normalized.sort, 'ai');
assert.equal(normalized.page_size, 25);

// Latest request wins, even inside one filter generation.
state = Review.initialState({ preset: 'everything' }, options);
state = reduce(state, { type: 'REQUEST_DAYS', generation: 0, requestId: 'days-1' });
state = reduce(state, { type: 'REQUEST_DAYS', generation: 0, requestId: 'days-2' });
state = reduce(state, { type: 'RECEIVE_DAYS', generation: 0, requestId: 'days-1', payload: { days: [{ day: 'old' }] } });
assert.deepEqual(state.days, []);
state = reduce(state, { type: 'RECEIVE_DAYS', generation: 0, requestId: 'days-2', payload: { days: [{ day: '2026-08-18' }], default_open_day: '2026-08-18' } });
assert.equal(state.days[0].day, '2026-08-18');
assert.equal(state.openDays['2026-08-18'], true);

// Collapsing while a refresh is loading is authoritative; a late response cannot reopen the day.
state = reduce(state, { type: 'REQUEST_DAYS', generation: 0, requestId: 'days-3' });
state = reduce(state, { type: 'TOGGLE_DAY', day: '2026-08-18', open: false });
state = reduce(state, { type: 'RECEIVE_DAYS', generation: 0, requestId: 'days-3', payload: { days: [{ day: '2026-08-18' }], default_open_day: '2026-08-18' } });
assert.equal(state.openDays['2026-08-18'], false);

// A filter transition invalidates the entire prior generation.
const previousGeneration = state.generation;
state = reduce(state, { type: 'SET_FILTER', field: 'q', value: 'new query', options });
assert.equal(state.generation, previousGeneration + 1);
const snapshot = state;
state = reduce(state, { type: 'RECEIVE_DAYS', generation: previousGeneration, requestId: 'stale', payload: { days: [{ day: 'stale' }] } });
assert.deepEqual(state, snapshot);

// Day pagination is stable, deduplicated, and protected from same-generation stale responses.
const generation = state.generation;
state = reduce(state, { type: 'REQUEST_DAY', generation, day: '2026-08-18', append: false, requestId: 'page-1' });
state = reduce(state, { type: 'REQUEST_DAY', generation, day: '2026-08-18', append: false, requestId: 'page-2' });
state = reduce(state, { type: 'RECEIVE_DAY', generation, day: '2026-08-18', append: false, requestId: 'page-1', payload: { items: [{ id: 99 }] } });
assert.deepEqual(state.dayData['2026-08-18'].items, []);
state = reduce(state, { type: 'RECEIVE_DAY', generation, day: '2026-08-18', append: false, requestId: 'page-2', payload: { items: [{ id: 1 }, { id: 2 }], next_cursor: 'c1', has_more: true } });
assert.deepEqual(state.dayData['2026-08-18'].items.map(item => item.id), [1, 2]);
state = reduce(state, { type: 'REQUEST_DAY', generation, day: '2026-08-18', append: true, requestId: 'page-3' });
state = reduce(state, { type: 'RECEIVE_DAY', generation, day: '2026-08-18', append: true, requestId: 'page-3', payload: { items: [{ id: 2 }, { id: 3 }], has_more: false } });
assert.deepEqual(state.dayData['2026-08-18'].items.map(item => item.id), [1, 2, 3]);

// Item detail responses are also latest-request-wins.
state = reduce(state, { type: 'REQUEST_ITEM', itemId: 1, requestId: 'item-1' });
state = reduce(state, { type: 'REQUEST_ITEM', itemId: 1, requestId: 'item-2' });
state = reduce(state, { type: 'RECEIVE_ITEM', itemId: 1, requestId: 'item-1', payload: { title: 'stale' } });
assert.equal(state.itemDetails[1].status, 'loading');
state = reduce(state, { type: 'RECEIVE_ITEM', itemId: 1, requestId: 'item-2', payload: { title: 'current' } });
assert.equal(state.itemDetails[1].data.title, 'current');

// Optimistic read transitions confirm or roll back exactly one mutation token.
state = reduce(state, { type: 'OPTIMISTIC_READ', itemId: 1, read: true, token: 'read-1' });
assert.equal(state.dayData['2026-08-18'].items[0].is_read, true);
const ignored = reduce(state, { type: 'ROLLBACK_READ', itemId: 1, token: 'old-token' });
assert.equal(ignored.dayData['2026-08-18'].items[0].is_read, true);
state = reduce(state, { type: 'ROLLBACK_READ', itemId: 1, token: 'read-1' });
assert.equal(state.dayData['2026-08-18'].items[0].is_read, false);
state = reduce(state, { type: 'OPTIMISTIC_READ', itemId: 1, read: true, token: 'read-2' });
state = reduce(state, { type: 'CONFIRM_READ', itemId: 1, token: 'read-2' });
assert.equal(state.dayData['2026-08-18'].items[0].is_read, true);
assert.equal(state.readMutations[1], undefined);

// Finish-day is single-flight and success is idempotent at the state level.
state = { ...state, days: [{ day: '2026-08-18', unread: 3, complete: false }], openDays: { '2026-08-18': true } };
state = reduce(state, { type: 'FINISH_DAY_START', day: '2026-08-18' });
const duplicateStart = reduce(state, { type: 'FINISH_DAY_START', day: '2026-08-18' });
assert.deepEqual(duplicateStart, state);
state = reduce(state, { type: 'FINISH_DAY_SUCCESS', day: '2026-08-18', payload: { changed: 3 } });
assert.equal(state.days[0].unread, 0);
assert.equal(state.days[0].complete, true);
assert.equal(state.openDays['2026-08-18'], false);
assert.equal(state.finishingDays['2026-08-18'], undefined);

console.log('review state transitions: ok');

// Cross-filter transitions prevent combinations that are semantically empty.
state = Review.initialState({ preset: 'best-unread' }, options);
state = reduce(state, { type: 'SET_FILTER', field: 'ai', value: 'pending', options });
assert.equal(state.filters.ai, 'pending');
assert.equal(state.filters.min_ai, 0);
assert.equal(state.filters.decision, 'all');
state = reduce(state, { type: 'SET_FILTER', field: 'min_ai', value: '90', options });
assert.equal(state.filters.ai, 'scored');
assert.equal(state.filters.min_ai, 90);
state = reduce(state, { type: 'SET_FILTER', field: 'ai', value: 'not-sent', options });
state = reduce(state, { type: 'SET_FILTER', field: 'decision', value: 'drop', options });
assert.equal(state.filters.ai, 'scored');
assert.equal(state.filters.decision, 'drop');

// Preset application is a complete query transition, not a visual-only selection.
const presetGeneration = state.generation;
state = reduce(state, { type: 'APPLY_PRESET', preset: 'today', options });
assert.equal(state.generation, presetGeneration + 1);
assert.equal(state.filters.from, '2026-08-18');
assert.equal(state.filters.to, '2026-08-18');
assert.deepEqual(state.dayData, {});

// Current request failures are visible; stale failures cannot overwrite a newer request.
state = reduce(state, { type: 'REQUEST_DAYS', generation: state.generation, requestId: 'failure-old' });
state = reduce(state, { type: 'REQUEST_DAYS', generation: state.generation, requestId: 'failure-current' });
const beforeStaleFailure = state;
state = reduce(state, { type: 'FAIL_DAYS', generation: state.generation, requestId: 'failure-old', error: 'stale' });
assert.deepEqual(state, beforeStaleFailure);
state = reduce(state, { type: 'FAIL_DAYS', generation: state.generation, requestId: 'failure-current', error: 'current failure' });
assert.equal(state.status, 'error');
assert.equal(state.error, 'current failure');

// Day/item failures and toggles have explicit recoverable states.
const failureGeneration = state.generation;
state = reduce(state, { type: 'REQUEST_DAY', generation: failureGeneration, day: '2026-08-18', requestId: 'day-fail' });
state = reduce(state, { type: 'FAIL_DAY', generation: failureGeneration, day: '2026-08-18', requestId: 'day-fail', error: 'page failure' });
assert.equal(state.dayData['2026-08-18'].loading, false);
assert.equal(state.dayData['2026-08-18'].error, 'page failure');
state = reduce(state, { type: 'TOGGLE_ITEM', itemId: 77, open: true });
assert.equal(state.openItems[77], true);
state = reduce(state, { type: 'REQUEST_ITEM', itemId: 77, requestId: 'item-fail' });
state = reduce(state, { type: 'FAIL_ITEM', itemId: 77, requestId: 'item-fail', error: 'detail failure' });
assert.equal(state.itemDetails[77].status, 'error');
assert.equal(state.itemDetails[77].error, 'detail failure');

// A read mutation is single-flight even when the control is activated twice.
state = {
  ...state,
  dayData: { '2026-08-18': { items: [{ id: 88, is_read: false }], nextCursor: '', hasMore: false } },
};
state = reduce(state, { type: 'OPTIMISTIC_READ', itemId: 88, read: true, token: 'single-1' });
const duplicateRead = reduce(state, { type: 'OPTIMISTIC_READ', itemId: 88, read: false, token: 'single-2' });
assert.deepEqual(duplicateRead, state);
state = reduce(state, { type: 'CONFIRM_READ', itemId: 88, token: 'single-1' });
assert.equal(state.readMutations[88], undefined);

// Finish-day failures clear the single-flight guard without changing completion.
state = { ...state, days: [{ day: '2026-08-18', unread: 2, complete: false }] };
state = reduce(state, { type: 'FINISH_DAY_START', day: '2026-08-18' });
state = reduce(state, { type: 'FINISH_DAY_FAIL', day: '2026-08-18', error: 'finish failure' });
assert.equal(state.finishingDays['2026-08-18'], undefined);
assert.equal(state.days[0].complete, false);
assert.equal(state.days[0].unread, 2);
assert.equal(state.error, 'finish failure');

console.log('review extended transitions: ok');

// Stale persisted open days are pruned so the current result can open its own default day.
state = Review.initialState({ preset: 'everything' }, options);
state = { ...state, openDays: { '2025-01-01': true } };
state = reduce(state, { type: 'REQUEST_DAYS', generation: 0, requestId: 'prune-open' });
state = reduce(state, {
  type: 'RECEIVE_DAYS', generation: 0, requestId: 'prune-open',
  payload: { days: [{ day: '2026-08-18' }], default_open_day: '2026-08-18' },
});
assert.deepEqual(state.openDays, { '2026-08-18': true });

console.log('review persisted-state transitions: ok');

// Group display modes are normalized and direct mode opens every unseen day.
assert.equal(Review.normalizeDisplayMode('DIRECT'), 'direct');
assert.equal(Review.normalizeDisplayMode('unknown'), 'daily');
let displayState = Review.initialState({ preset: 'catch-up' }, { ...options, displayMode: 'daily' });
displayState = reduce(displayState, { type: 'REQUEST_DAYS', generation: 0, requestId: 'display-days-1' });
displayState = reduce(displayState, {
  type: 'RECEIVE_DAYS', generation: 0, requestId: 'display-days-1',
  payload: {
    scope: { review_display_mode: 'direct' },
    days: [{ day: '2026-08-18' }, { day: '2026-08-17' }, { day: '2026-08-16' }],
    default_open_day: '2026-08-18',
  },
});
assert.equal(displayState.displayMode, 'direct');
assert.deepEqual(displayState.openDays, {
  '2026-08-18': true, '2026-08-17': true, '2026-08-16': true,
});

// A manual collapse is authoritative in direct mode, including across a late refresh.
displayState = reduce(displayState, { type: 'TOGGLE_DAY', day: '2026-08-17', open: false });
displayState = reduce(displayState, { type: 'REQUEST_DAYS', generation: 0, requestId: 'display-days-2' });
displayState = reduce(displayState, {
  type: 'RECEIVE_DAYS', generation: 0, requestId: 'display-days-2',
  payload: {
    scope: { review_display_mode: 'direct' },
    days: [{ day: '2026-08-18' }, { day: '2026-08-17' }, { day: '2026-08-16' }],
    default_open_day: '2026-08-18',
  },
});
assert.equal(displayState.openDays['2026-08-17'], false);
assert.equal(displayState.openDays['2026-08-16'], true);

// Loading data for a collapsed day never reopens it.
displayState = reduce(displayState, {
  type: 'REQUEST_DAY', generation: 0, day: '2026-08-17', append: false,
  requestId: 'collapsed-day-page',
});
displayState = reduce(displayState, {
  type: 'RECEIVE_DAY', generation: 0, day: '2026-08-17', append: false,
  requestId: 'collapsed-day-page', payload: { items: [{ id: 700 }], has_more: false },
});
assert.equal(displayState.openDays['2026-08-17'], false);
assert.equal(displayState.dayData['2026-08-17'].items[0].id, 700);

// Switching modes has a complete, deterministic open-day transition and leaves filters intact.
const filtersBeforeModeChange = displayState.filters;
displayState = reduce(displayState, { type: 'SET_DISPLAY_MODE', mode: 'daily' });
assert.equal(displayState.displayMode, 'daily');
assert.equal(displayState.openDays['2026-08-18'], true);
assert.equal(displayState.openDays['2026-08-17'], false);
assert.equal(displayState.openDays['2026-08-16'], false);
assert.deepEqual(displayState.filters, filtersBeforeModeChange);
displayState = reduce(displayState, { type: 'SET_DISPLAY_MODE', mode: 'direct' });
assert.equal(displayState.displayMode, 'direct');
assert.deepEqual(displayState.openDays, {
  '2026-08-18': true, '2026-08-17': true, '2026-08-16': true,
});

// The server remains authoritative if the mode changes in another tab.
displayState = reduce(displayState, { type: 'REQUEST_DAYS', generation: 0, requestId: 'display-days-3' });
displayState = reduce(displayState, {
  type: 'RECEIVE_DAYS', generation: 0, requestId: 'display-days-3',
  payload: {
    scope: { review_display_mode: 'daily' },
    days: [{ day: '2026-08-18' }, { day: '2026-08-17' }],
    default_open_day: '2026-08-18',
  },
});
assert.equal(displayState.displayMode, 'daily');
assert.deepEqual(displayState.openDays, { '2026-08-18': true });

console.log('review group display transitions: ok');
