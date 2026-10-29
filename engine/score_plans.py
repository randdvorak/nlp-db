"""Score predictions on the hand-written questions by running them (read-only) on the project's
test database (`test_db:`, else `train_db:`, else `conninfo:`).

  python score_plans.py --project databases/<name> plans predictions.jsonl   # one {"text": plan} per line
  python score_plans.py --project databases/<name> sql   predictions.jsonl   # one {"sql": ...} per line
Gold is the compiled gold plan. A prediction is right when its rows equal the gold rows
(as a multiset; columns may come in any order; numbers rounded to 2 places).
Writes <predictions>.failures.jsonl.
"""
import decimal, json, sys
from collections import Counter
import project as P
import compile as C


def cell(v):
    if isinstance(v, (float, decimal.Decimal)):
        return f"{round(float(v), 2):.2f}"
    return "" if v is None else str(v)


def rows_of(cur, sql):
    cur.execute(sql)
    return [tuple(cell(c) for c in r) for r in cur.fetchall()]


def same(a, b):
    if Counter(a) == Counter(b):
        return True
    return Counter(tuple(sorted(r)) for r in a) == Counter(tuple(sorted(r)) for r in b)


mode, path = sys.argv[1], sys.argv[2]
gold = [json.loads(l) for l in open(P.path("plans", "tests.jsonl"), encoding="utf-8")]
preds = [json.loads(l) for l in open(path, encoding="utf-8")]
cur = C.connect(P.TEST_DB).cursor()
right, fails, kinds = 0, [], Counter()
for g, p in zip(gold, preds):
    q = g["messages"][1]["content"]
    gold_plan = json.loads(g["messages"][2]["content"])
    if "not_tracked" in gold_plan or "not_tracked" in (p.get("text") or ""):
        # Unanswerable questions: right when the model says so, whatever its wording.
        try:
            said = "not_tracked" in json.loads(p.get("text") or "{}")
        except ValueError:
            said = False
        ok = said == ("not_tracked" in gold_plan)
        right += ok
        kinds["exact plan" if ok else "other"] += 1
        if not ok:
            fails.append({"question": q, "gold": g["messages"][2]["content"], "predicted": p.get("text", p.get("sql")),
                          "reason": "said it can't answer" if said else "should have said it can't answer"})
        continue
    gold_rows = rows_of(cur, g["sql"])
    try:
        if mode == "plans":
            plan = json.loads(p["text"])
            sql = C.compile_plan(plan)
            exact = plan == json.loads(g["messages"][2]["content"])
        else:
            sql, exact = p["sql"], False
        ok = same(rows_of(cur, sql), gold_rows)
        reason = "" if ok else "wrong rows"
    except Exception as e:  # noqa: BLE001
        cur.connection.rollback() if not cur.connection.autocommit else None
        sql, ok, exact, reason = p.get("sql", p.get("text")), False, False, f"{type(e).__name__}: {str(e).strip().splitlines()[0]}"
    right += ok
    kinds["exact plan" if exact else "other"] += 1
    if not ok:
        fails.append({"question": q, "gold": g["messages"][2]["content"], "predicted": p.get("text", p.get("sql")), "reason": reason})
print(f"{mode}: {right}/{len(gold)} right ({100 * right / len(gold):.1f}%)" + (f", exact plan match {kinds['exact plan']}" if mode == "plans" else ""))
with open(path.rsplit(".", 1)[0] + ".failures.jsonl", "w", encoding="utf-8") as f:
    for x in fails:
        f.write(json.dumps(x, ensure_ascii=False) + "\n")
