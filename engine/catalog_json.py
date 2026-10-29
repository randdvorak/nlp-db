"""catalog.yaml -> data/catalog.json for aq (Swift has no YAML parser). Structure only: no
data values; aq reads those from the database.

Lists keep their order (fact preference, node order, value order), and decode codes keep
their type (vendor ratings are numbers, product lines are letters), so aq compiles plans
to the same SQL as compile.py.
"""
import json
import os

import project as P

CAT = P.CATALOG


def nodes(item):
    n = item["node"]
    return n if isinstance(n, list) else [n]


def item(d):
    out = {"nodes": nodes(d), "sql": d["sql"]}
    if "values" in d:  # CASE-defined values (customer_type, channel, pay_type)
        out["values"] = [str(v) for v in d["values"]]
    if "decode" in d:
        out["decode"] = [[k if isinstance(k, int) else str(k), str(v)] for k, v in d["decode"].items()]
    return out


import compile as C  # noqa: E402  (for ENTITY_FACTS)
import values as V  # noqa: E402

cat = {
    # What aq says and where it connects; the system prompt must be the one the model was trained with.
    "project": {"name": P.NAME, "system_prompt": P.SYSTEM_PROMPT, "conninfo": P.CONNINFO,
                "model_file": P.SETTINGS.get("model_file", "model.gguf")},
    "nodes": [[a, t] for a, t in CAT["nodes"].items()],
    "joins": [[e[0], e[1], e[2], len(e) > 3 and e[3] == "optional"] for e in CAT["joins"]],
    "facts": [{"name": n, "base": f["base"], "date": f.get("date"),
               "measures": [[m, v["sql"]] for m, v in f["measures"].items()]} for n, f in CAT["facts"].items()],
    "dimensions": {n: item(d) for n, d in CAT["dimensions"].items()},
    "flags": {n: item(d) for n, d in CAT["flags"].items()},
    "grains": CAT["time"]["grains"],
    "periods": CAT["time"]["periods"],
    "entity_facts": C.ENTITY_FACTS,
    "list_nodes": C.LIST_NODES,
    # Where aq looks values up (no values here): [dim, "db" | "context" | "decode", context words].
    "lookup": [[d, k, w] for d, k, w in V.lookup_dims()],
    "common_words": sorted(V.COMMON),
    "not_tracked": CAT.get("not_tracked", []),
}
out = P.path("catalog.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(cat, f, ensure_ascii=False, separators=(",", ":"))
print(f"wrote {out} ({os.path.getsize(out) // 1024} KB)")
