"""catalog.yaml -> data/plan.gbnf: a llama.cpp grammar that only admits plans built from
catalog names (measures, dimensions, flags, periods), in the compact JSON the model is
trained on, or {"not_tracked": "..."} for things the database does not record. Values stay free strings or numbers; the compiler checks the rest.

  python3 plan_gbnf.py --project databases/<name>   # writes data/plan.gbnf (a reference: dbq builds its own copy
                                  # from catalog.json, see PlanGrammar.swift; dbq --print-grammar)
"""
import project as P

CAT = P.CATALOG


def q(name):
    # GBNF literal for the JSON string "name"
    return '"\\"' + name + '\\""'


def alt(names):
    return " | ".join(q(n) for n in sorted(names, key=lambda n: (-len(n), n)))


measures = {m for f in CAT["facts"].values() for m in f["measures"]}
dims = set(CAT["dimensions"])
grains = set(CAT["time"]["grains"])
flags = set(CAT["flags"])
periods = set(CAT["time"]["periods"])
reasons = set(CAT.get("not_tracked", []))

G = f'''# Plan grammar, built from the catalog's names (dbq builds the same at startup).
root ::= "{{" item ("," item)* "}}" | "{{\\"not_tracked\\":" reason "}}"
item ::= "\\"measures\\":[" measure ("," measure)* "]"
       | "\\"show\\":[" dim ("," dim)* "]"
       | "\\"group_by\\":[" group ("," group)* "]"
       | "\\"filters\\":[" filter ("," filter)* "]"
       | "\\"time\\":" time
       | "\\"having\\":[" having ("," having)* "]"
       | "\\"order_by\\":[" order ("," order)* "]"
       | "\\"limit\\":" int
measure ::= {alt(measures)}
dim ::= {alt(dims)}
group ::= {alt(dims | grains)}
flag ::= {alt(flags)}
reason ::= {alt(reasons)}
period ::= {alt(periods)}
filter ::= "{{\\"flag\\":" flag "}}"
         | "{{\\"dim\\":" group ",\\"op\\":" cmp ",\\"value\\":" value "}}"
         | "{{\\"dim\\":" group ",\\"op\\":\\"like\\",\\"value\\":" string "}}"
         | "{{\\"dim\\":" group ",\\"op\\":" ("\\"in\\"" | "\\"not in\\"") ",\\"value\\":[" value ("," value)* "]}}"
         | "{{\\"dim\\":" group ",\\"op\\":\\"between\\",\\"value\\":[" value "," value "]}}"
         | "{{\\"dim\\":" group ",\\"op\\":" ("\\"is null\\"" | "\\"is not null\\"") "}}"
cmp ::= "\\"=\\"" | "\\"<>\\"" | "\\">=\\"" | "\\"<=\\"" | "\\">\\"" | "\\"<\\""
time ::= "{{\\"period\\":" period ("," "\\"n\\":" int)? ("," "\\"from\\":" date ",\\"to\\":" date)? "}}"
having ::= "{{\\"measure\\":" measure ",\\"op\\":" cmp ",\\"value\\":" number "}}"
order ::= "{{\\"measure\\":" measure ",\\"dir\\":" dir "}}" | "{{\\"dim\\":" group ",\\"dir\\":" dir "}}"
dir ::= "\\"asc\\"" | "\\"desc\\""
value ::= string | number
date ::= "\\"" [0-9] [0-9] [0-9] [0-9] "-" [0-9] [0-9] "-" [0-9] [0-9] "\\""
string ::= "\\"" ([^"\\\\\\x7F\\x00-\\x1F] | "\\\\" ["\\\\/bfnrt])* "\\""
number ::= "-"? int ("." [0-9]+)?
int ::= [0-9]+
'''
out = P.path("plan.gbnf")
# llama.cpp wants each rule on one line unless the line break sits inside parentheses.
G = G.replace("\n       | ", " | ").replace("\n         | ", " | ")
with open(out, "w", encoding="utf-8") as f:
    f.write(G)
print(f"wrote {out}: {len(measures)} measures, {len(dims)} dimensions, {len(flags)} flags, {len(periods)} periods")
