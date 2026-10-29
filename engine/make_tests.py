"""The project's tests.py (a list Q of (question, gold plan)) -> data/tests.jsonl, checking that
every gold plan compiles.

  python3 make_tests.py --project databases/<name>
"""
import json
import os
import runpy
import sys

import project as P
import compile as C

src = os.path.join(P.DIR, "tests.py")
if not os.path.exists(src):
    sys.exit(f"no tests.py in {P.DIR}")
Q = runpy.run_path(src)["Q"]
out = P.path("tests.jsonl")
with open(out, "w", encoding="utf-8") as f:
    for q, p in Q:
        if "not_tracked" not in p:
            C.compile_plan(p)
        f.write(json.dumps({"question": q, "plan": p}, ensure_ascii=False) + "\n")
print(f"wrote {len(Q)} questions to {out}")
