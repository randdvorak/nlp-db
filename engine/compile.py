"""Compile a semantic plan (JSON) into one PostgreSQL SELECT, using the project's catalog.yaml.

A plan names only things from the catalog:
  {"measures": ["revenue"], "group_by": ["category", "year"],
   "filters": [{"dim": "territory", "op": "=", "value": "Canada"}, {"flag": "online"}],
   "time": {"period": "last_month"}            # or {"period": "year", "n": 2013}, {"period": "past_days", "n": 30},
                                                #    {"period": "between", "from": "2013-01-01", "to": "2013-03-31"}
   "having": [{"measure": "revenue", "op": ">", "value": 100000}],
   "order_by": [{"measure": "revenue", "dir": "desc"}],   # or {"dim": ...}
   "limit": 5}
A plan with no measures is a list: "show" names the columns, e.g.
  {"show": ["product", "list_price"], "filters": [{"dim": "category", "op": "=", "value": "Bikes"}],
   "order_by": [{"dim": "list_price", "dir": "desc"}], "limit": 5}
Group-by entries "year", "quarter", "month", "day" are time grains on the fact's date.

Usage:
  python3 compile.py --project DIR '<plan json>' [--run] [--db CONNINFO]
  python3 compile.py --project DIR --selftest [--db CONNINFO]   # compile and run every catalog entry
"""
import json
import re
import sys
from collections import deque

import project as P

CAT = P.CATALOG
GRAINS = CAT["time"]["grains"]
PERIODS = CAT["time"]["periods"]
OPS = {"=", "<>", ">", "<", ">=", "<=", "in", "not in", "like", "between", "is null", "is not null"}
# Facts that are lists of things rather than events: preferred for plans with no measures.
ENTITY_FACTS = CAT.get("entity_facts", []) + [n for n, f in CAT["facts"].items() if f.get("list") and n not in CAT.get("entity_facts", [])]


class PlanError(Exception):
    pass


class NotTracked(PlanError):
    """The question asks for something the database does not record: {"not_tracked": "what"}."""


# ---------- join graph ----------

EDGES = {}  # from -> [(to, on, optional)]
for e in CAT["joins"]:
    EDGES.setdefault(e[0], []).append((e[1], e[2], len(e) > 3 and e[3] == "optional"))


def reach(base):
    """BFS over many-to-one edges: node -> (parent, on, optional) for the shortest path."""
    tree = {base: None}
    queue = deque([base])
    while queue:
        n = queue.popleft()
        for to, on, opt in EDGES.get(n, []):
            if to not in tree:
                tree[to] = (n, on, opt)
                queue.append(to)
    return tree


REACH = {name: reach(f["base"]) for name, f in CAT["facts"].items()}
# A list ("show") can also start from any table, e.g. the category list starts from pcat.
# Role nodes (a buyer is one role of Person.Person) never start a list.
LIST_NODES = [n for n in CAT["nodes"] if n not in set(CAT.get("role_nodes", []))]
for _n in LIST_NODES:
    REACH["node:" + _n] = reach(_n)


def fact_def(fact):
    if fact.startswith("node:"):
        return {"base": fact[5:], "measures": {}}
    return CAT["facts"][fact]


def nodes_of(item):
    n = item["node"]
    return n if isinstance(n, list) else [n]


def sql_nodes(sql):
    """Aliases mentioned as alias.column in a SQL fragment (whole words: "doc." is not "pdoc.")."""
    return {a for a in CAT["nodes"] if re.search(rf"(?<![A-Za-z0-9_]){a}\.", sql)}


# ---------- pieces ----------

def lit(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def dim_expr(name, fact):
    if name in GRAINS:
        date = fact_def(fact).get("date")
        if not date:
            raise PlanError(f"{fact} has no date for '{name}'")
        return GRAINS[name].format(d=date)
    d = CAT["dimensions"][name]
    if "decode" in d:
        whens = " ".join(f"WHEN {lit(_code(d, k))} THEN {lit(v)}" for k, v in d["decode"].items())
        return f"CASE {d['sql']} {whens} END"
    return d["sql"]


def _code(d, k):
    # Codes stored as numbers stay numbers (vendor.CreditRating, poh.Status).
    return k if isinstance(k, int) else str(k)


def filter_value(name, v):
    """Map a spoken value back to the stored code for decoded dimensions."""
    d = CAT["dimensions"].get(name, {})
    if "decode" in d:
        back = {str(w).lower(): k for k, w in d["decode"].items()}
        if str(v).lower() in back:
            return _code(d, back[str(v).lower()])
    return v


def filter_sql(f, fact):
    if "flag" in f:
        flag = CAT["flags"].get(f["flag"])
        if not flag:
            raise PlanError(f"unknown flag '{f['flag']}'")
        return flag["sql"]
    name, op = f["dim"], f.get("op", "=").lower()
    if op not in OPS:
        raise PlanError(f"unknown operator '{op}'")
    d = CAT["dimensions"].get(name)
    if d is None and name not in GRAINS:
        raise PlanError(f"unknown dimension '{name}'")
    col = dim_expr(name, fact) if name in GRAINS else d["sql"]
    if op in ("is null", "is not null"):
        return f"{col} {op.upper()}"
    v = f.get("value")
    if op in ("in", "not in"):
        return f"{col} {op.upper()} (" + ", ".join(lit(filter_value(name, x)) for x in v) + ")"
    if op == "between":
        return f"{col} BETWEEN {lit(v[0])} AND {lit(v[1])}"
    if op == "like":
        return f"{col} ILIKE {lit('%' + str(v) + '%')}"
    return f"{col} {op} {lit(filter_value(name, v))}"


def time_sql(t, fact):
    date = fact_def(fact).get("date")
    if not date:
        raise PlanError(f"{fact} has no date column for a time filter")
    p = t["period"]
    if p not in PERIODS:
        raise PlanError(f"unknown period '{p}'")
    return PERIODS[p].format(d=date, n=int(t.get("n", 0)), **{"from": t.get("from", ""), "to": t.get("to", "")})


# ---------- fact choice ----------

def needed_nodes(plan):
    need = set()
    for name in plan.get("group_by", []) + plan.get("show", []):
        if name not in GRAINS:
            if name not in CAT["dimensions"]:
                raise PlanError(f"unknown dimension '{name}'")
            need |= set(nodes_of(CAT["dimensions"][name]))
    for f in plan.get("filters", []):
        item = CAT["flags"].get(f["flag"]) if "flag" in f else CAT["dimensions"].get(f.get("dim"))
        if item is None:
            if f.get("dim") in GRAINS:
                continue
            raise PlanError(f"unknown filter {json.dumps(f)}")
        need |= set(nodes_of(item))
    return need


def choose_fact(plan):
    measures = plan.get("measures", [])
    need = needed_nodes(plan)
    uses_time = bool(plan.get("time")) or any(g in GRAINS for g in plan.get("group_by", []))
    if plan.get("fact"):
        cands = [plan["fact"]]
    elif measures:
        cands = [n for n, f in CAT["facts"].items() if all(m in f["measures"] for m in measures)]
    else:
        cands = ENTITY_FACTS + [n for n in CAT["facts"] if n not in ENTITY_FACTS]
        if not uses_time:
            cands += ["node:" + n for n in LIST_NODES]
    ok = [n for n in cands
          if need <= set(REACH[n]) and (not uses_time or fact_def(n).get("date"))]
    if not ok:
        raise PlanError(f"no fact answers measures {measures} with {sorted(need)}")
    if measures:
        return ok[0]
    # For lists, the fact needing the fewest joins (a product list comes from products, not order lines).
    return min(ok, key=lambda n: (len(_path_nodes(n, need)), cands.index(n)))


def _path_nodes(fact, need):
    tree, out = REACH[fact], set()
    for n in need:
        while n is not None and n not in out:
            out.add(n)
            n = tree[n][0] if tree[n] else None
    return out


# ---------- compile ----------

def compile_plan(plan):
    if "not_tracked" in plan:
        if plan["not_tracked"] not in CAT.get("not_tracked", []):
            raise PlanError(f"unknown not_tracked reason {plan['not_tracked']!r}")
        raise NotTracked(f"can't answer from the database: {plan['not_tracked']}")
    fact = choose_fact(plan)
    f = fact_def(fact)
    measures = plan.get("measures", [])
    group = plan.get("group_by", [])
    show = plan.get("show", [])
    for m in measures:
        if m not in f["measures"]:
            raise PlanError(f"{fact} has no measure '{m}'")

    select = [f"{dim_expr(g, fact)} AS {g}" for g in group + show]
    select += [f"{f['measures'][m]['sql']} AS {m}" for m in measures]
    if not select:
        raise PlanError("plan selects nothing")

    where = [filter_sql(x, fact) for x in plan.get("filters", [])]
    if plan.get("time"):
        where.append(time_sql(plan["time"], fact))
    having = []
    for h in plan.get("having", []):
        if h["measure"] not in f["measures"]:
            raise PlanError(f"{fact} has no measure '{h['measure']}'")
        if h.get("op", ">") not in {"=", "<>", ">", "<", ">=", "<="}:
            raise PlanError(f"bad having operator '{h.get('op')}'")
        having.append(f"{f['measures'][h['measure']]['sql']} {h.get('op', '>')} {lit(h['value'])}")

    # Joins: every node any expression mentions, plus the path back to the base.
    text = " ".join(select + where + having + [f.get("date", "") if plan.get("time") or any(g in GRAINS for g in group) else ""])
    need = sql_nodes(text) | needed_nodes(plan)
    tree = REACH[fact]
    order, left = [], set()
    for n in sorted(_path_nodes(fact, need), key=lambda n: (_depth(tree, n), n)):
        if tree[n] is None:
            continue
        parent, on, opt = tree[n]
        if opt or parent in left:
            left.add(n)
        order.append(f"{'LEFT JOIN' if n in left else 'JOIN'} {CAT['nodes'][n]} {n} ON {on}")

    # A list never repeats a row: "which colors" means each color once.
    sql = f"SELECT {'' if measures else 'DISTINCT '}{', '.join(select)} FROM {CAT['nodes'][f['base']]} {f['base']}"
    if order:
        sql += " " + " ".join(order)
    if where:
        sql += " WHERE " + " AND ".join(where)
    if measures and (group or show):
        sql += " GROUP BY " + ", ".join(dim_expr(g, fact) for g in group + show)
    if having:
        sql += " HAVING " + " AND ".join(having)

    obys = plan.get("order_by")
    if obys is None:
        if plan.get("limit") and measures:
            obys = [{"measure": measures[0], "dir": "desc"}]
        elif any(g in GRAINS for g in group):
            obys = [{"dim": g} for g in group if g in GRAINS]
        else:
            obys = []
    parts = []
    for o in obys:
        name = o.get("measure") or o.get("dim")
        if name not in measures + group + show:
            raise PlanError(f"order_by '{name}' is not selected")
        parts.append(f"{name} {'DESC' if o.get('dir', 'asc').lower() == 'desc' else 'ASC'}")
    if parts:
        sql += " ORDER BY " + ", ".join(parts)
    if plan.get("limit"):
        sql += f" LIMIT {int(plan['limit'])}"
    return sql


def _depth(tree, n):
    d = 0
    while tree[n] is not None:
        n, d = tree[n][0], d + 1
    return d


# ---------- command line ----------

def connect(db=None):
    """Read-only connection. `db` is a libpq conninfo ("dbname=x host=y") or a bare database name."""
    import psycopg2
    db = db or P.CONNINFO
    conn = psycopg2.connect(db if "=" in db else f"dbname={db}",
                            options="-c default_transaction_read_only=on -c statement_timeout=30000")
    conn.autocommit = True
    return conn


def selftest(db):
    """Compile and run: every measure alone and by every reachable dimension, every flag, every period."""
    cur = connect(db).cursor()
    plans = []
    for fname, f in CAT["facts"].items():
        tree = REACH[fname]
        for i, m in enumerate(f["measures"]):
            plans.append({"fact": fname, "measures": [m]})
            # Every reachable dimension with the fact's first measure; the others alone and by time.
            for dname, d in CAT["dimensions"].items():
                if i == 0 and set(nodes_of(d)) <= set(tree):
                    plans.append({"fact": fname, "measures": [m], "group_by": [dname], "limit": 3})
            if f.get("date"):
                for g in GRAINS:
                    plans.append({"fact": fname, "measures": [m], "group_by": [g], "limit": 3})
        first = next(iter(f["measures"]))
        for flag, fl in CAT["flags"].items():
            if set(nodes_of(fl)) <= set(tree):
                plans.append({"fact": fname, "measures": [first], "filters": [{"flag": flag}]})
        if f.get("date"):
            for p in PERIODS:
                t = {"period": p, "n": 2013 if p == "year" else 30, "from": "2013-01-01", "to": "2013-03-31"}
                plans.append({"fact": fname, "measures": [first], "time": t})
    for dname, d in CAT["dimensions"].items():
        plans.append({"show": [dname], "limit": 3})
        for v in (d.get("values") or list((d.get("decode") or {}).values()))[:2]:
            plans.append({"show": [dname], "filters": [{"dim": dname, "op": "=", "value": v}], "limit": 3})
    ok = empty = 0
    bad = []
    for p in plans:
        try:
            sql = compile_plan(p)
            cur.execute(sql)
            rows = cur.fetchall()
            ok += 1
            if not rows or all(all(c is None for c in r) for r in rows):
                empty += 1
                print("EMPTY", json.dumps(p), "\n   ", sql)
        except Exception as e:  # noqa: BLE001 - report every failure
            bad.append((p, str(e).strip().splitlines()[0]))
    for p, e in bad:
        print("FAIL", json.dumps(p), "->", e)
    print(f"{ok}/{len(plans)} plans compiled and ran ({empty} returned no data), {len(bad)} failed")
    return not bad


def main(argv):
    db = P.CONNINFO
    if "--db" in argv:
        db = argv[argv.index("--db") + 1]
    if "--selftest" in argv:
        sys.exit(0 if selftest(db) else 1)
    plan = json.loads(argv[0])
    sql = compile_plan(plan)
    print(sql)
    if "--run" in argv:
        cur = connect(db).cursor()
        cur.execute(sql)
        print(" | ".join(c.name for c in cur.description))
        for r in cur.fetchmany(20):
            print(" | ".join("" if c is None else str(c) for c in r))


if __name__ == "__main__":
    main(sys.argv[1:])
