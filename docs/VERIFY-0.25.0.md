# Running the release verification

Use a disposable installation or a copy of the database, not the only production copy.
No test is meant to call a paid AI provider. Live resources are mocked in regression tests.

With the normal application dependencies and pytest installed:

    python -m pytest

The dependency-light subset exercised during the build is recorded verbatim in the
validation report. The large archive test generates a temporary >100 MiB ZIP and a
>200 MiB SQLite database; it does not ship large fixture files in the release.

Standalone browser state checks (Node.js):

    node tests/review_state.test.js
    node tests/reader025_state.test.js

The existing review_state.test.js includes the persisted-state and group-display suites.

Optional Playwright/Chromium harness:

    python tests/browser_reader025.py

This loads the actual Jinja template, CSS and browser scripts, connects its fetch
fixture to actual SQLite review/profile handlers, and intercepts external resources.
It is a browser/SQLite integration check, NOT a Flask HTTP check. Its temporary output
folder contains results and screenshots. Set DISTILLFEED_TEST_OUTPUT to choose a folder,
and DISTILLFEED_CHROMIUM to select an installed Chromium executable. It will create
browser.sqlite3 in that output folder, so do not point it at application data.

Tests test_web025.py exercise real Flask endpoints when the application dependencies
are available; that module is explicitly skipped in the dependency-limited build.
The legacy test-distillfeed.sh is the comprehensive isolated release installer/test
workflow. It installs its test dependencies, so it requires package-index access.
It is intentionally separate from the offline upd.sh installation transaction.
