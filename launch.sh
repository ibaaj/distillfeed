#!/bin/sh

set -eu
umask 077

ROOT=$(CDPATH= cd -P "$(dirname "$0")" && pwd)

if ! "$ROOT/install.sh" --check >/dev/null 2>&1; then
    DISTILLFEED_FROM_LAUNCH=1 "$ROOT/install.sh"
fi

cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m rss_reader.launcher --root "$ROOT" "$@"
