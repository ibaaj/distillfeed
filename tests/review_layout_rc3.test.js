'use strict';
const assert = require('node:assert/strict');
const State = require('../rss_reader/static/review-state.js');

// Date ranges now appear on the same line as the scope's result counts;
// the date fields remain server-side filters, not presentation-only state.
assert.equal(State.dateRangeLabel({from:'2026-10-19',to:'2026-10-24'}),'2026-10-19 – 2026-10-24');
assert.equal(State.dateRangeLabel({from:'2026-10-19'}),'From 2026-10-19');
assert.equal(State.dateRangeLabel({to:'2026-10-24'}),'Through 2026-10-24');
assert.equal(State.dateRangeLabel({from:'bad',to:'2026-10-24'}),'Through 2026-10-24');
assert.equal(State.dateRangeLabel({}),'');
const counts = {matching:3371,total:32899,unread:32828,pending:1100};
const filters = State.normalizeFilters({preset:'current-week'}, {today:'2026-10-24', isArxiv:true, minimumRelevance:80});
assert.equal(filters.from,'2026-10-19');
assert.equal(filters.to,'2026-10-24');
const label = State.countSummary(counts,filters,{today:'2026-10-24',isArxiv:true});
assert.match(label,/3,371 results/);
assert.match(label,/32,899 total/);
assert.match(label,/32,828 unread overall/);
assert.match(label,/1,100 awaiting AI/);
assert.equal(`${label} – ${State.dateRangeLabel(filters)}`.endsWith('– 2026-10-19 – 2026-10-24'),true);
// Inserting date text into the status does not alter filtering state.
const before = State.initialState(filters, {today:'2026-10-24',isArxiv:true});
const after = State.dateRangeLabel(before.filters);
assert.equal(after,'2026-10-19 – 2026-10-24');
assert.deepEqual(before.filters,State.initialState(filters,{today:'2026-10-24',isArxiv:true}).filters);
console.log('RC3 compact date/status state tests: PASS');
