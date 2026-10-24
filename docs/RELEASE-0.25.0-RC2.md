# DistillFeed 0.25.0 RC2

Build: 24 October 2026. Source baseline: the supplied 0.25.0 RC1 release.
The package version remains 0.25.0 for same-version installation. The static
resource revision and service worker cache are distinct: 0.25.0-rc2.

## Reader changes

- Current month covers the first calendar day of the month through today.
- The default view is adaptive for each selected group or feed: Current week
  if it contains dated items this week, otherwise Current month if it contains
  dated items this month, otherwise Everything. Read state does not restrict
  the decision. Existing explicit URL filters are preserved. Day keys are UTC,
  consistently with the existing reader and arXiv announcement partition.
- Changing the day layout does not change the semantic default view. Reset
  restores that source's initial adaptive view or the configured fixed view.
- AI interests are inside AI configuration for the selected ordinary source.
  The arXiv configuration control opens its existing specialist settings.
- Counts and active date range are next to the source heading. Item AI scores,
  View content and save/read controls share a wrapping compact toolbar.
- Expanded-item read-aloud and article-theatre controls join that same toolbar.
- Audio reading controls are hidden until requested; they can be explicitly
  hidden again. Closing them stops speech. Pause and Resume keep them open.
- The text slider affects both item titles and expanded content, previews
  immediately, and serializes saves so older responses cannot win. Failed
  saves are reported rather than represented as saved.

## General Settings: Reader & audio

Configurable fixed/adaptive default view; page size; compact/comfortable cards;
rationale visibility and line count; tags; scope statistics; local speech on/off;
audio panel initially open/closed; speech volume, rate, maximum segment length
and inter-segment pause; YouTube and article-theatre availability; default wide
media mode. Existing Appearance controls still set font sizes individually.
Settings changes take effect on closing Settings (page reload). The text slider
is immediate. New values use defaults without rewriting the user's config at
installation; explicit saves persist them atomically through existing Settings.

## Speech mitigation, not a confirmed hardware/audio fix

Initial or already-idle playback no longer calls speechSynthesis.cancel().
Segments prefer sentence boundaries, with a configurable gap (120 ms default).
Replacing active speech separates cancellation from the next request. Duplicate
or stale native callbacks cannot restart old text. Default speech volume is 0.65;
only browser-reported local voices are used, with no remote fallback.

Browser speech can still have platform or driver artifacts. This release does
not assert that the reported macOS/Firefox pop has been reproduced or eliminated.
Native speech synthesis exposes no reliable PCM fade control here. Reduce system
volume for a first short test; stop and disable read aloud if the noise returns.

## Preserved behavior

Titles still open the original article or arXiv abstract in a new tab. Cards do
not disappear merely because they were opened/read. Unknown announcement dates
stay last and collapsed. The canonical daily arXiv partition and historical
brief recovery are byte-for-byte unchanged from RC1, as are backup/restore and
feed handling. No new database migration or automatic rescoring is introduced.
Manual updates remain primary; automatic execution is not enabled by this update.
README.md, CITATION.cff and UPDATE-GITHUB.md remain protected by the Git updater.

## Verification and limits

See the bundle validation report for commands and actual results. The selected
Python, state-transition and Chromium tests use controlled data, not production
data. Chromium uses actual templates/scripts and SQLite-backed test transport;
it does not substitute for a full Flask HTTP or native macOS speech test.
No real provider calls, database edits on the user's machine, or GitHub pushes
are performed during building.
