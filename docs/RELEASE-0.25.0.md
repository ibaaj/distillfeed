# DistillFeed 0.25.0 — reader and recovery release candidate

## Source provenance

Built from the actual final-patched 0.24.2 source archive supplied in this conversation,
with the supplied canonical daily-partition hotfix incorporated, then the changes below.
The exact previously claimed 0.24.3 source archive and the user's production database
were not available. The supplied 0.24.3 validation report ends in a failed assertion.
The prior intended date, respectful challenge handling, scoring provenance and Shorts
behaviour has therefore been implemented and tested explicitly here, not assumed.

This is a candidate, not a claim of zero bugs or an executed production deployment.
README.md is unchanged. The GitHub updater protects the checkout's README.md,
CITATION.cff and UPDATE-GITHUB.md. No real GitHub repository is modified by the build.

## Reading workflow

Current week is the fresh-view default (Monday through today). Last 5 days includes
today and the four preceding calendar days. Both use the reader's canonical UTC day
keys and include read and unread entries. An old catch-up bookmark becomes Last 5 days.
An explicit saved URL remains explicit; Reset filters or reopen the group to get the
new default. Best, Best unread and Best read select AI-kept entries at/above the
configured minimum relevance; the minimum score remains adjustable. Today, Awaiting
AI, Starred and Everything remain available. No Catch up menu entry remains.

Opening, marking, or favoriting a loaded card does not reset the stream. Even an
unread-only loaded card is retained after clicking so it can be reread in that session;
a subsequent explicit filter change or reload applies that filter authoritatively.
The read marker and counters still change. Open content and open daily briefs survive
item and pagination updates. Failed page loads retain prior cards and permit retry.

Counts, day discovery, filters and cursor pages execute in SQLite. Day pages contain
at most 60 headers; initial item pages default to 25 compact cards. Full source text
loads on expansion. Everything no longer transfers all historical abstracts to Python
before filtering. Equal-score pagination is stable and cursors are scope/filter-bound.

Unknown arXiv announcement dates are last, initially collapsed, and excluded from
explicit date windows. The UI calls them Announcement date unknown, not moderated or
bad papers. No announcement date is guessed from submission time or a weekday rule.

A two-line AI relevance preview precedes tags. View content, favorite, read-later and
read controls sit together on the left. Local matching details are a collapsed
secondary disclosure. Item titles still open the canonical source in a new tab.
Ordinary group/feed update buttons include the selection's name.

## Historical arXiv recovery

Readiness, stored score selection, evidence fingerprints and generation use the same
canonical announcement-day policy. Complete stored evaluations remain usable for
brief reconstruction even if their current local score is below today's screening
threshold. There is no historical age cutoff for missing known-date daily briefs.
Existing scores are reused. Successful earlier days commit separately; a failed later
day can be retried. Unknown dates do not inflate the historical-known-day warning.
Existing legacy timestamps with no authoritative announcement evidence remain unknown.

New/revised paper eligibility still uses the configured local threshold. Request batch
size is not a total candidate cap. Newly scored papers carry model/rubric/fingerprint/
run provenance. The existing configured model is not changed or benchmarked here.
The rubric uses fixed relevance bands and independent-paper instructions. Existing
AI scores are not reset just to add provenance or preferences.

## Backup restoration

Compressed ZIP limit: 1 GiB. Expanded SQLite member limit: 4 GiB. Uploaded files are
streamed rather than read entirely into memory. The ZIP member set, types, sizes and
CRC, manifest, SQLite integrity and foreign keys are checked before replacement.
Schema/plugin migration runs against the staged database. A safety ZIP and rollback
snapshot are created. Local configuration and credentials are not replaced by backup
reference configuration. The maintenance lease remains active across replacement.
A simulated post-swap OPML write failure is tested to restore the prior database.
Allow sufficient free disk for the archive, staged database, safety archive and rollback
copy. Proxy upload limits and server timeouts may be lower than the application limit.

Offline alternative, with the application stopped:

    .venv/bin/python -m rss_reader.restore --config config.toml --backup /path/to/backup.zip

The command requires an existing local configuration, not a raw unconfigured setup.
It preserves that configuration. It is not a database synchronization mechanism.

## Source-specific interests

Ordinary groups/feeds have a collapsible AI interests editor. The hierarchy is feed,
nearest non-empty parent group, then global interests. Empty text inherits. Up to 8,000
characters are accepted. Each job snapshots the effective source profile; later edits
do not mutate a running job. Profiles are included in entry-scoring payloads and cost
estimates. They apply to subsequent eligible evaluations, not an automatic rerank of
all old items. OPML export carries the optional distillfeedInterests attribute; imports
without it preserve the existing preference. arXiv retains its separate specialist
prompt/settings rather than inheriting ordinary-feed profiles.

## Playback, theatre and read aloud

Explicit YouTube URLs yield a click-only player using youtube-nocookie.com. /shorts/
URLs receive a Shorts badge; watch URLs are not guessed to be Shorts. Embeds are not
created during ordinary feed refresh or initial page rendering. No autoplay is forced.
The dialog supports a wider theatre layout, Escape/Close cleanup and an external link.

Article theatre attempts a sandboxed HTTPS iframe and always offers Open original.
Publishers can block framing, sign-in or video playback. DistillFeed does not proxy,
scrape, remove frame restrictions or bypass paywalls/protection. Cross-origin article
text is not available for read aloud: the stored item text remains the readable source.

Read aloud applies to stored item content, daily briefs and titles already loaded in
open days. It uses browser SpeechSynthesis voices marked localService=true only.
A browser that exposes no local system voice reports unavailability rather than sending
text to a cloud voice. Voice choice, pause/resume/stop, chunked reading, stale-event
cancellation and media/dialog cleanup are implemented. Actual macOS audio output is a
local acceptance check, not something verified in this Linux build environment.

## Cloud folders and scheduling

No concurrent Proton Drive/other file-sync database sharing is added. Keep the live
SQLite database local. Exported OPML or completed backup ZIPs may be transferred between
computers, with deliberate import/restore and one authoritative writer. A synchronized
live database is not made safe by ordinary SQLite file locks. Automatic scheduling
remains off by default; the updater preserves existing user configuration.

## Verification and limitations

See VALIDATION-0.25.0.txt in the outer release bundle for exact results. SQLite regression
fixtures, archive restore, schema migration, JavaScript reducers, real Chromium with a
SQLite-backed transport fixture, template compilation and packaging are exercised.
The fixture is not Flask HTTP. Flask, Werkzeug, feedparser and the OpenAI SDK are missing
from the build runtime. The complete dependency-bearing suite and full updater switch
therefore remain unexecuted here. Browser tests stub external resources; they do not
validate live YouTube playback, publisher-specific frames or macOS voice quality.
Never treat passing sampled state transitions as a proof that no other defect exists.

## Reference documentation

https://developer.mozilla.org/en-US/docs/Web/API/SpeechSynthesisVoice/localService
https://developers.google.com/youtube/player_parameters
https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Frame-Options
https://www.sqlite.org/useovernet.html
