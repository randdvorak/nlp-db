#!/bin/bash
# Score dbq on the project's hand-written questions, running plans on its test_db:
#   scripts/evaluate.sh databases/<name>
# Also checks that dbq compiles plans and finds value hints exactly like the Python engine.
source "$(dirname "$0")/common.sh"
DBQ="$ROOT/dbq/.build/release/dbq"; WORK="$DBQ_PROJECT/work"; mkdir -p "$WORK"
DB="$(conninfo TEST_DB)"
"$DBQ" --check-file "$DBQ_PROJECT/data/plans/valid.jsonl" --db "$DB" > "$WORK/dbq_check.jsonl"
(cd "$ROOT/engine" && "$PY" check_parity.py "$DBQ_PROJECT/data/plans/valid.jsonl" "$WORK/dbq_check.jsonl" --db "$DB")
"$DBQ" --eval "$DBQ_PROJECT/data/plans/tests.jsonl" --db "$DB" > "$WORK/dbq_tests.jsonl"
(cd "$ROOT/engine" && "$PY" score_plans.py plans "$WORK/dbq_tests.jsonl")
