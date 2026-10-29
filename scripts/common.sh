# Sourced by the other scripts: resolves the project folder and reads its settings.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -ge 1 ] || { echo "usage: $(basename "$0") databases/<name>" >&2; exit 2; }
export DBQ_PROJECT="$(cd "$1" && pwd)"
PY="${PYTHON:-python3}"
setting() {  # setting EXPR: a value from engine/project.py, e.g. setting TEST_DB
    (cd "$ROOT/engine" && "$PY" -c "import project as P; print(P.$1)")
}
conninfo() {  # conninfo NAME: a database setting as a libpq conninfo
    local v; v="$(setting "$1")"
    if [[ "$v" == *=* ]]; then echo "$v"; else echo "dbname=$v"; fi
}
