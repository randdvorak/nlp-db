#!/bin/bash
# Score aq on the project's hand-written questions, running plans on its test_db:
#   scripts/evaluate.sh databases/<name>
# Also checks that aq compiles plans and finds value hints exactly like the Python engine.
source "$(dirname "$0")/common.sh"
AQ="$ROOT/aq/.build/release/aq"; WORK="$AQ_PROJECT/work"; mkdir -p "$WORK"
DB="$(conninfo TEST_DB)"
"$AQ" --check-file "$AQ_PROJECT/data/plans/valid.jsonl" --db "$DB" > "$WORK/aq_check.jsonl"
(cd "$ROOT/engine" && "$PY" check_parity.py "$AQ_PROJECT/data/plans/valid.jsonl" "$WORK/aq_check.jsonl" --db "$DB")
"$AQ" --eval "$AQ_PROJECT/data/plans/tests.jsonl" --db "$DB" > "$WORK/aq_tests.jsonl"
(cd "$ROOT/engine" && "$PY" score_plans.py plans "$WORK/aq_tests.jsonl")
