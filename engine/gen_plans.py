"""Generate (question, plan) training pairs from the project's catalog.yaml and phrasing.yaml.

Each pair starts from a plan sampled from the catalog (a fact, its measures, dimensions it
can reach, filters with real values, a time window), which compile.py turns into SQL
and runs (read-only), so every pair is known to work. The question is written from the
plan with varied wording. The model learns to map wording to catalog names, and the
compiler does the SQL.

Plans run on the project's train_db. Filter values are made up (see made_up_values in
phrasing.yaml), so the model never sees the database's own names.

  python3 gen_plans.py --project databases/<name> [--n 16000] [--db CONNINFO] [--seed 7]
writes data/pairs.jsonl: {"question", "prompt", "plan", "sql", "kind", "rows"}
"""
import argparse
import json
import os
import random
import sys

import project as P
import compile as C
import values as V

CAT = C.CAT
PH = P.PHRASING
DIMS = CAT["dimensions"]
COUNTS = {}  # distinct values per lookup dimension (load_numbers): only to tell big dimensions from small ones
CODE_LEN = {}  # lookup dimensions whose values are short codes ("CA", "USD"): the longest code's length
LOOKUP = {d: k for d, k, _ in V.lookup_dims()}

# ---------------- wording ----------------

PLURAL = PH.get("plural", {})


def plural(w):
    if w in PLURAL:
        return PLURAL[w]
    if w.endswith("person"):
        return w[:-6] + "people"
    if w.endswith("y") and w[-2:-1] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("s", "x", "ch", "sh")):
        return w + "es"
    return w + "s"


def say(item):
    # Noun forms only: "how many orders" is a question, not a name for the measure.
    nouns = [w for w in item["say"] if not w.startswith("how ")]
    return random.choice(nouns or item["say"])


# Measures: how people ask for them. {x} is the rest of the question.
ASK = PH.get("ask", {})
GENERIC_ASK = ["what was the {m} {x}", "what is the total {m} {x}", "show {m} {x}", "{m} {x}", "give me the {m} {x}",
               "tell me the {m} {x}", "what were {m} {x}", "can you show me {m} {x}", "I need the {m} {x}", "how much {m} {x}"]
BREAKDOWN_PLURAL = ["show the {dp} and their {m} {x}", "list {dp} with their {m} {x}", "{dp} and their total {m} {x}",
                    "each {d} with its {m} {x}"]
BREAKDOWN = ["{m} by {d} {x}", "show {m} per {d} {x}", "break down {m} by {d} {x}", "what is the {m} for each {d} {x}",
             "{m} for every {d} {x}", "give me {m} grouped by {d} {x}", "{m} split by {d} {x}", "list {m} by {d} {x}"]
COUNT_BREAKDOWN = ["{ask} per {d}", "{ask} by {d}", "{ask} for each {d}", "{ask}, broken down by {d}"]
TOP = ["top {n} {dp} by {m} {x}", "which {n} {dp} had the most {m} {x}", "the {n} best {dp} by {m} {x}",
       "show the top {n} {dp} ranked by {m} {x}", "what are the top {n} {dp} by {m} {x}", "{n} {dp} with the highest {m} {x}"]
# Plural with no number ("which countries get the most orders") means a top 10, not a single winner.
TOP_NONUM = ["which {dp} have the most {m} {x}", "{dp} with the most {m} {x}", "what are the top {dp} by {m} {x}",
             "which {dp} get the most {m} {x}", "top {dp} by {m} {x}", "our biggest {dp} by {m} {x}"]
DEFAULT_TOP = 10
TOP1 = ["which {d} had the most {m} {x}", "which {d} has the highest {m} {x}", "what is the top {d} by {m} {x}", "best {d} by {m} {x}",
        "which {d} brought in the most {m} {x}", "which single {d} had the most {m} {x}", "the {d} with the most {m} {x}",
        "which {d} was number one for {m} {x}", "what was the best {d} for {m} {x}"]
BOTTOM = ["bottom {n} {dp} by {m} {x}", "which {n} {dp} had the lowest {m} {x}", "the {n} worst {dp} by {m} {x}", "{n} {dp} with the least {m} {x}"]
HAVING = ["{dp} with {m} over {v} {x}", "which {dp} had more than {v} in {m} {x}", "{dp} where {m} exceeds {v} {x}",
          "list {dp} whose {m} is above {v} {x}"]
LIST = ["list {ep} {x}", "show all {ep} {x}", "which {ep} {x}", "what {ep} are there {x}", "give me the {ep} {x}", "find {ep} {x}"]
LIST_ATTR = ["list {ep} with their {a} {x}", "show {ep} and {a} {x}", "{ep} {x} with {a}", "give me each {e} and its {a} {x}"]
LIST_TOP = ["top {n} {ep} by {a} {x}", "the {n} {ep} with the highest {a} {x}", "{n} {ep} with the largest {a} {x}", "show the {n} {ep} with the most {a} {x}"]
LIST_BOTTOM = ["the {n} {ep} with the lowest {a} {x}", "{n} {ep} with the smallest {a} {x}", "bottom {n} {ep} by {a} {x}"]
GRAIN_WORDS = {"year": ["by year", "per year", "each year", "yearly", "annually"],
               "quarter": ["by quarter", "per quarter", "each quarter", "quarterly"],
               "month": ["by month", "per month", "each month", "monthly", "month by month"],
               "day": ["by day", "per day", "daily", "each day"]}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
TIME_WORDS = {
    "today": ["today", "so far today"],
    "this_month": ["this month", "so far this month", "month to date"],
    "last_month": ["last month", "in the previous month", "during last month", "for last month"],
    "this_quarter": ["this quarter", "so far this quarter", "quarter to date"],
    "last_quarter": ["last quarter", "in the previous quarter", "for last quarter"],
    "this_year": ["this year", "so far this year", "year to date", "YTD"],
    "last_year": ["last year", "in the previous year", "during last year", "for last year"],
    "past_days": ["in the past {n} days", "over the last {n} days", "in the last {n} days", "during the past {n} days"],
    "past_months": ["in the past {n} months", "over the last {n} months", "in the last {n} months"],
    "year": ["in {n}", "during {n}", "for {n}", "in the year {n}"],
    "between": ["between {from} and {to}", "from {from} to {to}", "from {fromh} through {toh}"],
}
FLAG_WORDS = PH.get("flag_words", {})
FILTER_WORDS = PH.get("filter_words", {})
GENERIC_FILTER = ["with {s} {v}", "where the {s} is {v}", "whose {s} is {v}", "with a {s} of {v}"]
# Numbers that are identifiers: filtered with "=", never ranges or rankings.
ID_DIMS = PH.get("id_dims", {})
NO_RANGE = set(ID_DIMS) | set(PH.get("no_range", []))
NULL_WORDS = PH.get("null_words", {})
NUMERIC_WORDS = {">": ["over", "above", "more than", "greater than"], "<": ["under", "below", "less than"]}
LIKE_DIMS = {d: v["phrases"] for d, v in PH.get("like", {}).items()}
LIKE_WORDS = {d: v["words"] for d, v in PH.get("like", {}).items()}
NUMERIC_RANGES = dict(PH.get("numeric_ranges", {}))
POLITE = ["", "", "", "", "please ", "quick question: "]
ASKING = ["can you tell me ", "I'd like to know ", "do you know "]

# Dimensions that make sense as a GROUP BY, and those with so many values a ranking needs a LIMIT.
NOT_GROUPABLE = set(PH.get("not_groupable", []))
GROUPABLE = [d for d, v in DIMS.items() if v.get("kind") not in ("number", "date", "text") and d not in NOT_GROUPABLE]
MANY = set(PH.get("many", []))
LISTABLE_MANY = set(PH.get("listable_many", []))
# Dimensions people ask about most: weighted up as filters and groups.
COMMON_DIMS = set(PH.get("common_dims", []))
ENTITY_SHOW = {f: (v["entity"], v["attrs"]) for f, v in PH.get("lists", {}).items()}
for _f, _d in CAT["facts"].items():
    if _d.get("list") and _f not in ENTITY_SHOW:
        ENTITY_SHOW[_f] = (_d["list"]["entity"], _d["list"]["attrs"])
FACT_WEIGHTS = dict(PH.get("fact_weights", {}))
NEW_FACT_WEIGHT = PH.get("new_fact_weight", 1)
for _f in CAT["facts"]:
    FACT_WEIGHTS.setdefault(_f, NEW_FACT_WEIGHT)
LISTS_WEIGHT = PH.get("lists_weight", 7) if ENTITY_SHOW else 0
LIST_WEIGHTS = PH.get("list_weights", {})  # others 1
NO_TIME_FACTS = set(PH.get("no_time_facts", []))
COUNT_MEASURES = set(PH.get("count_measures", []))
OPPOSITE_FLAGS = [set(p) for p in PH.get("opposite_flags", [])]
# Counting a fact grouped by its own entity ("actors by number of actors") is 1 per row:
# skipped unless phrasing.yaml keeps it (AdventureWorks round 5 data had it). OWN_ENTITY:
# fact -> dimensions on its base table with (nearly) one value per row (load_numbers).
SKIP_OWN_ENTITY = PH.get("skip_own_entity", True)
OWN_ENTITY = {}
YEARS = PH.get("years")  # [first, last]; load_numbers fills it from the data when missing

def reachable(fact, names):
    tree = C.REACH[fact]
    return [n for n in names if set(C.nodes_of(DIMS[n] if n in DIMS else CAT["flags"][n])) <= set(tree)]


def pick_group(fact, names):
    """Round-2 dimensions stay common; dimensions many joins away (an address's store's sales
    person's territory) are rare, since few people ask for them."""
    tree = C.REACH[fact]
    def depth(n):
        d = 0
        while tree[n] is not None:
            n, d = tree[n][0], d + 1
        return d
    def w(g):
        far = max(depth(n) for n in C.nodes_of(DIMS[g]))
        return (3 if g in COMMON_DIMS else 1) * (0.15 if far > 3 else 1)
    return random.choices(names, weights=[w(g) for g in names])[0]


# ---------------- made-up values ----------------
# Training never uses the database's own names (customers, products, places...). Filters get
# invented values, and the "Values:" hint lists them the way dbq's live lookup would, so the
# model learns to copy what the lookup found instead of learning the data. Code words that
# the catalog itself defines (product line "Mountain", customer type "Store") stay as they are.
MADE_UP = PH.get("made_up_values", {})
SYL = MADE_UP.get("syllables") or ["zor", "van", "kel", "ith", "mar", "dra", "quin", "bel", "tor", "esh", "lun", "vex", "ora",
                                   "pim", "sal", "tek", "rho", "nai", "fen", "gul", "bra", "cor", "dax", "el", "fin", "gar"]
GROUPS = MADE_UP.get("groups", {})
# Dimensions whose real values look alike, so the live lookup finds one value under several of them.
SIBLINGS = [GROUPS[g[0]] if len(g) == 1 and g[0] in GROUPS else g for g in MADE_UP.get("siblings", [])]
LEVELS = MADE_UP.get("levels", [])
GENERIC_SHAPE = "f'{word()} {word(1)}' if rand() < 0.6 else word(3)"
SHAPES = {k: compile(v, f"made_up_values.shapes.{k}", "eval") for k, v in MADE_UP.get("shapes", {}).items()}
SHAPES[None] = compile(GENERIC_SHAPE, "generic shape", "eval")

def _word(n=2):
    return "".join(random.choice(SYL) for _ in range(n)).capitalize()


def fake_value(dim):
    if dim in CODE_LEN:  # shaped like the column's codes: "QX", "ZRL"
        return "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(random.randint(min(2, CODE_LEN[dim]), CODE_LEN[dim])))
    shape = SHAPES.get(dim) or next((SHAPES[g] for g, dims in GROUPS.items() if dim in dims and g in SHAPES), SHAPES[None])
    return str(eval(shape, {"word": _word, "pick": random.choice, "randint": random.randint, "rand": random.random,
                            "value": fake_value, **MADE_UP.get("names", {})}))

FAKE_POOL = {}


def value_for(dim):
    """A value for a filter: invented for database values, the catalog's own word for codes."""
    d = DIMS[dim]
    if dim in ID_VALUES:  # an id ("store 2"): made up in the column's range
        return random.choice(ID_VALUES[dim])
    if "decode" in d or "values" in d:
        vals = [str(v) for v in d["decode"].values()] if "decode" in d else d["values"]
        return random.choice(vals)
    if dim not in LOOKUP:
        return None
    pool = FAKE_POOL.setdefault(dim, [])
    if len(pool) < 400 or random.random() < 0.3:
        v = fake_value(dim)
        pool.append(v)
        return v
    return random.choice(pool)


def fake_hints(plan):
    """(dim, value) pairs the live lookup would find for the plan's made-up values, with the
    same kind of look-alikes it finds for real ones ("Canada" is a territory and a country)."""
    extra = []
    for f in plan.get("filters", []):
        d, v = f.get("dim"), f.get("value")
        if d not in LOOKUP or LOOKUP[d] == "decode" or v is None:
            continue
        for x in v if isinstance(v, list) else [v]:
            if not isinstance(x, str):
                continue
            extra.append((d, x))
            for level in LEVELS:
                if d in level and random.random() < 0.6:
                    extra.extend((other, x) for other in level if other != d and other in LOOKUP)
            for group in SIBLINGS:
                if d in group and random.random() < 0.6:
                    for other in random.sample(group, k=min(len(group), random.choice([1, 1, 2, 3]))):
                        if other != d and other in LOOKUP:
                            extra.append((other, x))
    return extra


def money(v):
    return random.choice([f"{v:,}", f"${v:,}", f"{v}", f"{v // 1000}k" if v >= 1000 and v % 1000 == 0 else f"{v:,}"])


# ---------------- filters and time ----------------

def pick_filters(fact, avoid, k):
    out, words = [], []
    flags = reachable(fact, list(CAT["flags"]))
    dims = [d for d in reachable(fact, list(DIMS)) if d not in avoid and filterable(d)]
    for _ in range(k):
        r = random.random()
        if r < 0.3 and flags:
            fl = random.choice(flags)
            if any(f.get("flag") == fl for f in out):
                continue
            # online/reseller and the like are opposites: never both.
            out.append({"flag": fl})
            words.append(random.choice(FLAG_WORDS.get(fl) or CAT["flags"][fl]["say"]))
            flags = [f for f in flags if f != fl and not _opposite(f, fl)]
        elif r < 0.36 and [d for d in reachable(fact, list(NULL_WORDS)) if d not in avoid]:
            d = random.choice([d for d in reachable(fact, list(NULL_WORDS)) if d not in avoid])
            out.append({"dim": d, "op": "is null"})
            words.append(random.choice(NULL_WORDS[d]))
            avoid = avoid | {d}
        elif r < 0.46:
            d = random.choice([d for d in reachable(fact, list(LIKE_DIMS))] or [None])
            if not d or d in avoid:
                continue
            v = random.choice(LIKE_WORDS[d])
            out.append({"dim": d, "op": "like", "value": v})
            words.append(random.choice(LIKE_DIMS[d]).format(v=v))
            avoid = avoid | {d}
        elif dims:
            d = random.choices(dims, weights=[filter_weight(x) for x in dims])[0]
            dims = [x for x in dims if x != d]
            if d in ID_DIMS:
                v = random.choice(ID_VALUES.get(d) or [None])
                if v is None:
                    continue
                out.append({"dim": d, "op": "=", "value": v})
                words.append(random.choice(ID_DIMS[d]).format(v=v))
                continue
            if d in NUMERIC_RANGES:
                op = random.choice([">", "<"])
                v = random.choice(NUMERIC_RANGES[d])
                out.append({"dim": d, "op": op, "value": v})
                words.append(f"with {say(DIMS[d])} {random.choice(NUMERIC_WORDS[op])} {v}")
                continue
            v = value_for(d)
            if v is None:
                continue
            if random.random() < 0.12 and (d in LOOKUP and LOOKUP[d] != "decode" or COUNTS.get(d, 0) > 2):
                v2 = value_for(d)
                if v2 != v:
                    out.append({"dim": d, "op": "in", "value": [v, v2]})
                    words.append(random.choice(filter_words(d)).format(v=f"{v} or {v2}", lv=f"{v} or {v2}".lower()))
                    continue
            out.append({"dim": d, "op": "=", "value": v})
            words.append(random.choice(filter_words(d)).format(v=v, lv=str(v).lower()))
    return out, words


NUMERIC_RANGES_FIXED = set(NUMERIC_RANGES)
ID_VALUES = {}


def filterable(d):
    kind = DIMS[d].get("kind")
    if d in ID_DIMS or d in NUMERIC_RANGES:
        return True
    if kind in ("number", "date", "text"):
        return False
    return d in FILTER_WORDS or d in LOOKUP or "decode" in DIMS[d] or "values" in DIMS[d]


def filter_weight(d):
    """Common dimensions (common_dims) show up most, ones with their own wording next, the rest less."""
    return 3 if d in COMMON_DIMS else 1 if d in FILTER_WORDS or d in ID_DIMS else 0.6


def filter_words(d):
    if d in FILTER_WORDS:
        return FILTER_WORDS[d]
    return [t.replace("{s}", say(DIMS[d])) for t in GENERIC_FILTER]


def load_numbers(cur):
    """Thresholds for number dimensions without hand-picked ones (quartiles, rounded), sample ids,
    and how many distinct values each lookup dimension has (a count, never the values)."""
    for d, kind, _ in V.lookup_dims():
        if kind != "decode":
            cur.execute(f"SELECT count(*), max(length(t.v::text)) FROM ({C.compile_plan({'show': [d]})}) t(v)")
            COUNTS[d], longest = cur.fetchone()
            if longest and longest <= 3:
                CODE_LEN[d] = longest
    MANY.update(d for d in GROUPABLE if COUNTS.get(d, 0) > 30)
    if SKIP_OWN_ENTITY:
        for fact, f in CAT["facts"].items():
            cur.execute(f"SELECT count(*) FROM {CAT['nodes'][f['base']]}")
            rows = cur.fetchone()[0]
            OWN_ENTITY[fact] = {d for d in DIMS if C.nodes_of(DIMS[d]) == [f["base"]]
                                and (d in ID_DIMS or rows and COUNTS.get(d, 0) >= 0.9 * rows)}
    global YEARS
    if not YEARS:
        dates = [f["date"] for f in CAT["facts"].values() if f.get("date")]
        ends = []
        for n, f in CAT["facts"].items():
            if f.get("date"):
                cur.execute(f"SELECT min(t.y), max(t.y) FROM ({C.compile_plan({'measures': [next(iter(f['measures']))], 'group_by': ['year']})}) t(y, m)")
                ends += [y for y in cur.fetchone() if y is not None]
        YEARS = [int(min(ends)), int(max(ends))] if ends else [2020, 2026]
    for d, v in DIMS.items():
        if v.get("kind") != "number" and d not in ID_DIMS or d in NUMERIC_RANGES_FIXED:
            continue
        cur.execute(C.compile_plan({"show": [d]}))
        nums = sorted(float(r[0]) for r in cur.fetchall() if r[0] is not None)
        if d in ID_DIMS:
            # Made-up ids in the column's range, not the database's own.
            ID_VALUES[d] = [random.Random(3 + i).randint(int(nums[0]), int(nums[-1])) for i in range(40)]
            continue
        if d in NO_RANGE or len(nums) < 3:
            continue
        cuts = sorted({_round2(nums[int(len(nums) * q)]) for q in (0.25, 0.5, 0.75)})
        NUMERIC_RANGES[d] = [c for c in cuts if c != 0] or cuts


def _round2(x):
    if x == 0:
        return 0
    import math
    digits = 1 - int(math.floor(math.log10(abs(x))))
    r = round(x, digits)
    return int(r) if r == int(r) else r


def _opposite(a, b):
    return {a, b} in OPPOSITE_FLAGS


def pick_time(fact):
    if not CAT["facts"].get(fact, {}).get("date"):
        return None, None
    p = random.choices(list(TIME_WORDS), weights=[0.3, 1, 1.5, 0.6, 0.8, 1.2, 1.5, 1, 0.8, 2, 0.8])[0]
    t = {"period": p}
    fill = {}
    if p == "past_days":
        t["n"] = fill["n"] = random.choice([7, 14, 30, 60, 90, 180, 365])
    elif p == "past_months":
        t["n"] = fill["n"] = random.choice([2, 3, 6, 9, 12, 18, 24])
    elif p == "year":
        t["n"] = fill["n"] = random.choice(range(YEARS[0], YEARS[1] + 1))
    elif p == "between" and random.random() < 0.5:
        # A single named month: "in March 2026".
        y, m1 = random.choice(range(YEARS[0], YEARS[1] + 1)), random.randint(1, 12)
        last = [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m1 - 1]
        t["from"], t["to"] = f"{y}-{m1:02d}-01", f"{y}-{m1:02d}-{last:02d}"
        return t, random.choice(["in {m} {y}", "during {m} {y}", "for {m} {y}", "in {m} of {y}"]).format(m=MONTHS[m1 - 1], y=y)
    elif p == "between":
        y = random.choice(range(YEARS[0], YEARS[1] + 1))
        m1 = random.randint(1, 11)
        m2 = random.randint(m1, 12)
        last = [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m2 - 1]
        t["from"], t["to"] = f"{y}-{m1:02d}-01", f"{y}-{m2:02d}-{last:02d}"
        fill = {"from": t["from"], "to": t["to"], "fromh": f"{MONTHS[m1 - 1]} {y}", "toh": f"{MONTHS[m2 - 1]} {y}"}
    return t, random.choice(TIME_WORDS[p]).format(**fill)


def ctx(words):
    random.shuffle(words)
    return " ".join(w for w in words if w)


# ---------------- plan kinds ----------------

def measure_plan(fact):
    f = CAT["facts"][fact]
    m = random.choice(list(f["measures"]))
    mw = say(f["measures"][m])
    kind = random.choices(["scalar", "breakdown", "time_series", "top", "bottom", "having", "two_dims", "versus", "spend"],
                          weights=[3, 3, 1.5, 3, 0.8, 0.8, 0.8, 0.7 if VERSUS else 0, 0.5 if fact in SPEND.get("facts", []) else 0])[0]
    if kind == "versus":
        vs = [d for d in reachable(fact, list(VERSUS)) if d in DIMS]
        if vs:
            return versus_plan(fact, m, mw, random.choice(vs))
        kind = "scalar"
    if kind == "spend":
        return spend_plan(fact)
    groupable = reachable(fact, GROUPABLE)
    if SKIP_OWN_ENTITY:
        groupable = [g for g in groupable if g not in OWN_ENTITY.get(fact, ())]
    plan = {"measures": [m]}
    group = []
    if kind in ("breakdown", "top", "bottom", "having", "two_dims"):
        if not groupable:
            kind = "scalar"
        else:
            group = [pick_group(fact, groupable)]
            if kind == "two_dims":
                other = [g for g in groupable if g != group[0] and g not in MANY]
                if not other:
                    kind = "breakdown"
                else:
                    group.append(pick_group(fact, other))
    if kind == "time_series" and (not f.get("date") or fact in NO_TIME_FACTS):
        kind = "scalar"
    if kind == "breakdown" and group[0] in MANY:
        kind = "top"
    filters, fwords = pick_filters(fact, set(group), random.choices([0, 1, 2, 3, 4], weights=[5, 4, 2, 1.2, 0.5])[0])
    t, twords = (None, None)
    if f.get("date") and fact not in NO_TIME_FACTS and random.random() < (0.3 if kind == "time_series" else 0.55):
        t, twords = pick_time(fact)
    x = ctx(fwords + [twords])
    if kind == "time_series":
        g = random.choice(list(GRAIN_WORDS))
        plan["group_by"] = [g]
        q = random.choice(GENERIC_ASK + list(ASK.get(m, []))).format(m=mw, x=x + " " + random.choice(GRAIN_WORDS[g]))
    elif kind == "scalar":
        q = random.choice(ASK.get(m, []) + GENERIC_ASK if m in ASK and random.random() < 0.7 else GENERIC_ASK).format(m=mw, x=x)
        if m in ASK and random.random() < 0.7:
            q = random.choice(ASK[m]).format(x=x)
    else:
        plan["group_by"] = group
        d = say(DIMS[group[0]])
        dp = plural(d)
        if kind in ("breakdown", "two_dims"):
            if kind == "two_dims":
                d = f"{d} and {say(DIMS[group[1]])}"
            if m in ASK and random.random() < 0.5:
                q = random.choice(COUNT_BREAKDOWN).format(ask=random.choice(ASK[m]).format(x=x).strip(), d=d)
            elif kind == "breakdown" and random.random() < 0.25:
                q = random.choice(BREAKDOWN_PLURAL).format(m=mw, d=d, dp=dp, x=x)
            else:
                q = random.choice(BREAKDOWN).format(m=mw, d=d, x=x)
        elif kind in ("top", "bottom"):
            n = random.choice([1, 1, 3, 5, 5, 10, 10, 20])
            plan["order_by"] = [{"measure": m, "dir": "desc" if kind == "top" else "asc"}]
            plan["limit"] = n
            if kind == "top" and random.random() < 0.25:
                plan["limit"] = DEFAULT_TOP
                q = random.choice(TOP_NONUM).format(dp=dp, m=mw, x=x)
            elif n == 1 and kind == "top":
                q = random.choice(TOP1).format(d=d, m=mw, x=x)
            else:
                q = random.choice(TOP if kind == "top" else BOTTOM).format(n=n, dp=dp, m=mw, x=x)
        else:  # having
            v = random.choice([5, 10, 50, 100, 500, 1000] if "count" in m or "units" in m or m in COUNT_MEASURES else [10000, 50000, 100000, 500000, 1000000])
            plan["having"] = [{"measure": m, "op": ">", "value": v}]
            plan["order_by"] = [{"measure": m, "dir": "desc"}]
            q = random.choice(HAVING).format(dp=dp, m=mw, v=money(v) if v >= 10000 else v, x=x)
    # Sometimes two measures at once: "orders and revenue by territory".
    others = [o for o in f["measures"] if o != m]
    if kind in ("scalar", "breakdown", "time_series", "two_dims") and others and random.random() < 0.15:
        m2 = random.choice(others)
        plan["measures"].append(m2)
        mw2 = say(f["measures"][m2])
        if f" {mw} " in f" {q} ":
            q = q.replace(mw, f"{mw} and {mw2}", 1)
        else:
            plan["measures"].pop()
    if filters:
        plan["filters"] = filters
    if t:
        plan["time"] = t
    return plan, q, kind


VERSUS = PH.get("versus", {})  # None: made-up names (database values)
SPEND = PH.get("spend", {})


# Made-up names a question mentions outside its filters ("Torab Gear vs Vercys"): the live lookup
# would list them too. Set by versus_plan, read and cleared by main.
EXTRA_HINTS = []


def versus_plan(fact, m, mw, d):
    opts = VERSUS[d][:] if VERSUS[d] else list(dict.fromkeys(value_for(d) for _ in range(random.choice([2, 3]))))
    if not VERSUS[d]:
        EXTRA_HINTS.extend((d, v) for v in opts)
    random.shuffle(opts)
    pair = " vs ".join(opts[:random.choice([2, len(opts)])]) if len(opts) > 2 else " vs ".join(opts)
    filters, fwords = pick_filters(fact, {d}, random.choices([0, 1], weights=[3, 1])[0])
    t, twords = pick_time(fact) if CAT["facts"][fact].get("date") and fact not in NO_TIME_FACTS and random.random() < 0.4 else (None, None)
    x = ctx(fwords + [twords])
    q = random.choice(["{m} {p} {x}", "{p} {m} {x}", "compare {m} {p} {x}", "how does {m} compare {p} {x}"]).format(m=mw, p=pair, x=x)
    plan = {"measures": [m], "group_by": [d]}
    if filters:
        plan["filters"] = filters
    if t:
        plan["time"] = t
    return plan, q, "versus"


def spend_plan(fact):
    d = random.choice(SPEND["dims"])
    v = value_for(d)
    t, twords = pick_time(fact) if random.random() < 0.4 else (None, None)
    q = random.choice(SPEND["questions"]).format(v=v, t=twords or "")
    plan = {"measures": [SPEND["measure"]], "filters": [{"dim": d, "op": "=", "value": v}]}
    if t:
        plan["time"] = t
    return plan, q, "spend"


LIST_SUPERLATIVES = PH.get("list_superlatives", {})


def list_plan():
    lf = list(ENTITY_SHOW)
    fact = random.choices(lf, weights=[LIST_WEIGHTS.get(f, 1) for f in lf])[0]
    ent, attrs = ENTITY_SHOW[fact]
    e = say(DIMS[ent])
    ep = plural(e)
    kind = random.choices(["list", "list_attr", "list_top", "list_bottom", "lookup", "attr_lookup", "related_list"],
                          weights=[2, 2, 2, 0.8, 1, 1.5, 1.5])[0]
    if kind == "attr_lookup":
        v = value_for(ent)
        if v is not None:
            a = random.choice(attrs)
            plan = {"show": [a], "filters": [{"dim": ent, "op": "=", "value": v}]}
            aw = say(DIMS[a])
            q = random.choice(["what is the {a} of {v}", "what's {v}'s {a}", "show the {a} for {v}", "{a} of {v}",
                               "tell me the {a} for {v}", "look up the {a} of {e} {v}"]).format(a=aw, v=v, e=e)
            return plan, q, kind
        kind = "list"
    if kind == "related_list":
        f2 = random.choice([f for f in CAT["facts"] if f not in ENTITY_SHOW] + list(ENTITY_SHOW))
        shows = [d for d in reachable(f2, GROUPABLE)]
        if shows:
            d = random.choice(shows)
            filters, fwords = pick_filters(f2, {d}, random.choices([1, 2], weights=[3, 1])[0])
            if filters:
                plan = {"show": [d], "filters": filters}
                q = random.choice(LIST).format(ep=plural(say(DIMS[d])), x=ctx(fwords))
                return plan, q, kind
        kind = "list"
    if kind == "lookup":
        d = random.choice([g for g in GROUPABLE if g not in MANY or g in LISTABLE_MANY])
        plan = {"show": [d]}
        q = random.choice(["list all {dp}", "what {dp} are there", "show the {dp}", "which {dp} do we have", "list the different {dp}"]).format(dp=plural(say(DIMS[d])))
        return plan, q, kind
    plan = {"show": [ent]}
    filters, fwords = pick_filters(fact, {ent}, random.choices([0, 1, 2, 3], weights=[1, 4, 1.5, 0.7])[0])
    nums = [a for a in attrs if DIMS[a].get("kind") in ("number", "date")]
    if kind in ("list_top", "list_bottom") and nums:
        a = random.choice(nums)
        n = random.choice([1, 3, 5, 10, 10, 20])
        plan["show"].append(a)
        plan["order_by"] = [{"dim": a, "dir": "desc" if kind == "list_top" else "asc"}]
        plan["limit"] = n
        words = LIST_SUPERLATIVES.get(a, {}).get("top" if kind == "list_top" else "bottom", [])
        if words and random.random() < 0.6:
            q = f"the {n} {random.choice(words)} {ep} {ctx(fwords)}" if n > 1 else f"the {random.choice(words)} {e} {ctx(fwords)}"
        else:
            q = random.choice(LIST_TOP if kind == "list_top" else LIST_BOTTOM).format(n=n, ep=ep, a=say(DIMS[a]), x=ctx(fwords))
    elif kind == "list_attr":
        a = random.choice(attrs)
        plan["show"].append(a)
        q = random.choice(LIST_ATTR).format(ep=ep, e=e, a=say(DIMS[a]), x=ctx(fwords))
    else:
        q = random.choice(LIST).format(ep=ep, x=ctx(fwords))
    if filters:
        plan["filters"] = filters
    return plan, q, kind


# Questions the database cannot answer: the plan says so instead of guessing a near miss
# (full-time status is not salaried, returns are not scrap).
NOT_TRACKED = [(x["reason"], x["questions"]) for x in PH.get("not_tracked_questions", [])]
PERSON_DIM = PH.get("person_dim")

def not_tracked_plan():
    what, qs = random.choice(NOT_TRACKED)
    return {"not_tracked": what}, random.choice(qs).replace("{p}", fake_value(PERSON_DIM)), "not_tracked"


KEY_ORDER = ["not_tracked", "measures", "show", "group_by", "filters", "time", "having", "order_by", "limit"]
NOT_TRACKED_SHARE = 0.02 if NOT_TRACKED else 0

# Common ways to ask for one winner ("which sales rep sold the most": limit 1) or a short ranking,
# and the manager relation in both directions.
SUPERLATIVE = [(x["measure"], x["dim"], x["one"], x.get("many", [])) for x in PH.get("superlatives", [])]
HIERARCHY = PH.get("hierarchy")
SPECIAL_SHARE = 0.06 if SUPERLATIVE or HIERARCHY else 0

def special_plan():
    if HIERARCHY and (not SUPERLATIVE or random.random() < 0.3):
        member, parent = HIERARCHY["member"], HIERARCHY["parent"]
        if random.random() < 0.5:
            v = value_for(parent)
            return {"show": [member], "filters": [{"dim": parent, "op": "=", "value": v}]}, random.choice(HIERARCHY["down"]).format(v=v), "reports_to"
        v = value_for(member)
        return {"show": [parent], "filters": [{"dim": member, "op": "=", "value": v}]}, random.choice(HIERARCHY["up"]).format(v=v), "manager_of"
    m, d, one, many = random.choice(SUPERLATIVE)
    fact = C.choose_fact({"measures": [m], "group_by": [d]})
    filters, fwords = pick_filters(fact, {d}, random.choices([0, 1], weights=[3, 1])[0])
    t, twords = pick_time(fact) if random.random() < 0.6 else (None, None)
    x = ctx(fwords + [twords])
    plan = {"measures": [m], "group_by": [d], "order_by": [{"measure": m, "dir": "desc"}], "limit": 1}
    if many and random.random() < 0.35:
        n = random.choice([3, 5, 10, 20])
        plan["limit"] = n
        q = random.choice(many).format(n=n, x=x)
    else:
        q = random.choice(one).format(x=x)
    if filters:
        plan["filters"] = filters
    if t:
        plan["time"] = t
    return plan, q, "superlative"


def canonical(plan):
    return {k: plan[k] for k in KEY_ORDER if k in plan}


def tidy(q):
    q = " ".join(q.replace(" ,", ",").split())
    q = random.choice(POLITE + (ASKING if q.split()[0].lower() in ("how", "what", "which", "who") else [])) + q
    q = q[0].upper() + q[1:] if random.random() < 0.7 else q
    return q + random.choice(["?", "?", "", "."]) if not q.endswith("?") else q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=16000)
    ap.add_argument("--db", default=P.TRAIN_DB)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=P.path("pairs.jsonl"))
    args = ap.parse_args()
    random.seed(args.seed)
    cur = C.connect(args.db).cursor()
    load_numbers(cur)
    matcher = V.Matcher()  # no database: training hints are made up (fake_hints)
    facts, weights = zip(*FACT_WEIGHTS.items())
    seen, out, stats = set(), [], {"fail": 0, "empty_dropped": 0}
    while len(out) < args.n:
        EXTRA_HINTS.clear()
        if random.random() < NOT_TRACKED_SHARE:
            plan, q, kind = not_tracked_plan()
        elif random.random() < SPECIAL_SHARE:
            plan, q, kind = special_plan()
        elif random.random() < LISTS_WEIGHT / (LISTS_WEIGHT + sum(weights)):
            plan, q, kind = list_plan()
        else:
            plan, q, kind = measure_plan(random.choices(facts, weights)[0])
        q = tidy(q)
        if q.lower() in seen:
            continue
        plan = canonical(plan)
        if kind == "not_tracked":
            # Each wording at most a few times (with different politeness), so the share stays small.
            seen.add(q.lower())
            out.append({"question": q, "prompt": V.prompt(q, matcher), "plan": plan, "sql": None, "kind": kind, "rows": 0})
            continue
        try:
            sql = C.compile_plan(plan)
            cur.execute(sql)
            rows = cur.fetchmany(50)
        except Exception as e:  # noqa: BLE001
            stats["fail"] += 1
            if stats["fail"] < 10:
                print("FAIL", json.dumps(plan), e, file=sys.stderr)
            continue
        extra = fake_hints(plan) + EXTRA_HINTS
        empty = not rows or all(all(c is None for c in r) for r in rows)
        # Made-up values match nothing, so empty results are expected there.
        if empty and not extra and random.random() < 0.85:
            stats["empty_dropped"] += 1
            continue
        seen.add(q.lower())
        out.append({"question": q, "prompt": V.prompt(q, matcher, extra=extra), "plan": plan, "sql": sql, "kind": kind, "rows": len(rows)})
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    kinds = {}
    for r in out:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"wrote {len(out)} pairs to {args.out}; {stats}; kinds {kinds}")


if __name__ == "__main__":
    main()
