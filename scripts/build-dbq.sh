#!/bin/bash
# Build dbq for one project: embeds the project's catalog (structure only, no data values)
# and links llama.cpp statically.
#   scripts/build-dbq.sh databases/<name>     -> dbq/.build/release/dbq
# Needs Xcode's Swift (/usr/bin/swift), cmake, and libpq (Homebrew).
source "$(dirname "$0")/common.sh"
(cd "$ROOT/engine" && "$PY" catalog_json.py)
"$PY" "$ROOT/dbq/scripts/embed-data.py" "$DBQ_PROJECT"
[ -d "$ROOT/dbq/vendor/llama/lib" ] || bash "$ROOT/dbq/scripts/build-llama.sh"
cd "$ROOT/dbq" && "${SWIFT:-/usr/bin/swift}" build -c release
echo "built $ROOT/dbq/.build/release/dbq"
