/// The llama.cpp grammar for plans, built from the catalog's names at startup (the same text
/// engine/plan_gbnf.py writes). Only catalog names fit; values are free strings or numbers.
enum PlanGrammar {
    static let template = #"""
# Plan grammar, built from the catalog's names (dbq builds the same at startup).
root ::= "{" item ("," item)* "}" | "{\"not_tracked\":" reason "}"
item ::= "\"measures\":[" measure ("," measure)* "]" | "\"show\":[" dim ("," dim)* "]" | "\"group_by\":[" group ("," group)* "]" | "\"filters\":[" filter ("," filter)* "]" | "\"time\":" time | "\"having\":[" having ("," having)* "]" | "\"order_by\":[" order ("," order)* "]" | "\"limit\":" int
measure ::= {MEASURES}
dim ::= {DIMS}
group ::= {GROUPS}
flag ::= {FLAGS}
reason ::= {REASONS}
period ::= {PERIODS}
filter ::= "{\"flag\":" flag "}" | "{\"dim\":" group ",\"op\":" cmp ",\"value\":" value "}" | "{\"dim\":" group ",\"op\":\"like\",\"value\":" string "}" | "{\"dim\":" group ",\"op\":" ("\"in\"" | "\"not in\"") ",\"value\":[" value ("," value)* "]}" | "{\"dim\":" group ",\"op\":\"between\",\"value\":[" value "," value "]}" | "{\"dim\":" group ",\"op\":" ("\"is null\"" | "\"is not null\"") "}"
cmp ::= "\"=\"" | "\"<>\"" | "\">=\"" | "\"<=\"" | "\">\"" | "\"<\""
time ::= "{\"period\":" period ("," "\"n\":" int)? ("," "\"from\":" date ",\"to\":" date)? "}"
having ::= "{\"measure\":" measure ",\"op\":" cmp ",\"value\":" number "}"
order ::= "{\"measure\":" measure ",\"dir\":" dir "}" | "{\"dim\":" group ",\"dir\":" dir "}"
dir ::= "\"asc\"" | "\"desc\""
value ::= string | number
date ::= "\"" [0-9] [0-9] [0-9] [0-9] "-" [0-9] [0-9] "-" [0-9] [0-9] "\""
string ::= "\"" ([^"\\\x7F\x00-\x1F] | "\\" ["\\/bfnrt])* "\""
number ::= "-"? int ("." [0-9]+)?
int ::= [0-9]+
"""#

    /// GBNF alternatives for the JSON strings "name", longest first (as plan_gbnf.py orders them).
    static func alt<S: Sequence>(_ names: S) -> String where S.Element == String {
        Set(names).sorted { ($0.count, $1) > ($1.count, $0) }.map { #""\""# + $0 + #"\"""# }.joined(separator: " | ")
    }

    static func build(_ cat: Catalog) -> String {
        let dims = Set(cat.dims.keys)
        let fill = [
            "{MEASURES}": alt(cat.facts.flatMap { $0.measures.keys }),
            "{DIMS}": alt(dims),
            "{GROUPS}": alt(dims.union(cat.grains.keys)),
            "{FLAGS}": alt(cat.flags.keys),
            "{REASONS}": alt(cat.notTrackedReasons),
            "{PERIODS}": alt(cat.periods.keys),
        ]
        var g = template
        for (k, v) in fill { g = g.replacingOccurrences(of: k, with: v) }
        return g + "\n"
    }
}
