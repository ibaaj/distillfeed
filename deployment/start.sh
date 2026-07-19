#!/bin/sh
set -eu
CONFIG="${RSSREADER_CONFIG:-/var/data/config.toml}"
mkdir -p "$(dirname "$CONFIG")"
if [ ! -f "$CONFIG" ]; then
  cp /app/config.example.toml "$CONFIG"
fi
export RSSREADER_CONFIG="$CONFIG"
export DISTILLFEED_MODE="${DISTILLFEED_MODE:-production}"
exec gunicorn --workers 1 --threads "${WEB_THREADS:-8}" --timeout 300 --bind "0.0.0.0:${PORT:-8080}" rss_reader.wsgi:app
