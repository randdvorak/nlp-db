#!/bin/bash
# Everything before training, for one project folder:
#   scripts/prepare.sh databases/<name>
# checks the catalog against the database, writes data/catalog.json and data/plan.gbnf,
# the hand-written tests (if the project has tests.py), the generated training pairs and
# the MLX chat files in data/plans/. All database access is read-only.
source "$(dirname "$0")/common.sh"
cd "$ROOT/engine"
"$PY" coverage.py || echo "(coverage: the columns above are neither in the catalog nor excluded)"
"$PY" compile.py --selftest --db "$(conninfo TRAIN_DB)"
"$PY" catalog_json.py
"$PY" plan_gbnf.py
[ -f "$AQ_PROJECT/tests.py" ] && "$PY" make_tests.py
"$PY" gen_plans.py ${GEN_ARGS:-}
"$PY" make_plan_dataset.py
