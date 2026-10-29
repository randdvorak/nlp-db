"""Check that dbq's Swift port compiles plans and finds value hints exactly like compile.py and values.py.

  dbq --check-file data/plans/valid.jsonl > dbq_check.jsonl
  python3 check_parity.py --project databases/<name> data/plans/valid.jsonl dbq_check.jsonl [--db CONNINFO]
"""
import json, sys
import compile as C
import values as V

gold = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
swift = [json.loads(l) for l in open(sys.argv[2], encoding="utf-8")]
m = V.Matcher(C.connect(sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else None))
bad_sql = bad_prompt = 0
for g, s in zip(gold, swift):
    q = g["messages"][1]["content"].split("\nValues: ")[0]
    plan = json.loads(g["messages"][2]["content"])
    if "not_tracked" in plan:
        if s.get("not_tracked") != plan["not_tracked"]:
            bad_sql += 1
            print("not_tracked differs:", plan, s)
        sql = None
    else:
        sql = C.compile_plan(plan)
    if sql is not None and s.get("sql") != sql:
        bad_sql += 1
        if bad_sql <= 5:
            print("SQL differs:\n  py:   ", sql, "\n  swift:", s.get("sql") or s.get("error"))
    if s["prompt"] != V.prompt(q, m):
        bad_prompt += 1
        if bad_prompt <= 5:
            print("prompt differs:\n  py:   ", repr(V.prompt(q, m)), "\n  swift:", repr(s["prompt"]))
print(f"{len(gold)} lines: {bad_sql} SQL differences, {bad_prompt} prompt differences")
