#!/bin/bash
# Build aq for one project: embeds the project's catalog (structure only, no data values)
# and links llama.cpp statically.
#   scripts/build-aq.sh databases/<name>     -> aq/.build/release/aq
# Needs Xcode's Swift (/usr/bin/swift), cmake, and libpq (Homebrew).
source "$(dirname "$0")/common.sh"
(cd "$ROOT/engine" && "$PY" catalog_json.py)
"$PY" "$ROOT/aq/scripts/embed-data.py" "$AQ_PROJECT"
[ -d "$ROOT/aq/vendor/llama/lib" ] || bash "$ROOT/aq/scripts/build-llama.sh"
cd "$ROOT/aq" && "${SWIFT:-/usr/bin/swift}" build -c release
echo "built $ROOT/aq/.build/release/aq"
