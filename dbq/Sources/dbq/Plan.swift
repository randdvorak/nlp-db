import Foundation

// The semantic layer: a plan names measures, dimensions, flags and periods from the catalog
// (databases/<name>/catalog.yaml, exported to catalog.json) and is compiled here to one SELECT.
// This is a port of engine/compile.py and values.py; keep the two in step.

struct PlanError: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}

final class Catalog {
    struct Fact {
        let name: String
        let base: String
        let date: String?
        let measures: [String: String]
    }
    struct Item {
        let nodes: [String]
        let sql: String
        let decode: [(code: Any, word: String)]
    }
    struct Link {
        let parent: String?
        let on: String
        let optional: Bool
    }

    let nodeOrder: [String]
    let table: [String: String]
    let facts: [Fact]
    let dims: [String: Item]
    let flags: [String: Item]
    let grains: [String: String]
    let periods: [String: String]
    let entityFacts: [String]
    let listNodes: [String]
    let notTrackedReasons: Set<String>
    /// The project: name for messages, the system prompt the model was trained with, the default
    /// connection and the model file name.
    let name: String
    let systemPrompt: String
    let conninfo: String
    let modelFile: String
    /// Where values are looked up: "db" (database, anywhere), "context" (database, only next to
    /// one of `words`), "decode" (the catalog's own code meanings). No data values ship here.
    let lookup: [(dim: String, kind: String, words: [String])]
    /// CASE-defined values (customer_type, channel, pay_type).
    let caseValues: [String: [String]]
    let commonWords: [String]
    private(set) var reach: [String: [String: Link]] = [:]

    init(json: String) throws {
        guard let root = try JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any] else {
            throw PlanError("catalog is not a JSON object")
        }
        let nodes = root["nodes"] as? [[String]] ?? []
        nodeOrder = nodes.map { $0[0] }
        table = Dictionary(uniqueKeysWithValues: nodes.map { ($0[0], $0[1]) })
        facts = (root["facts"] as? [[String: Any]] ?? []).map { f in
            let ms = (f["measures"] as? [[String]] ?? [])
            return Fact(name: f["name"] as! String, base: f["base"] as! String, date: f["date"] as? String,
                        measures: Dictionary(uniqueKeysWithValues: ms.map { ($0[0], $0[1]) }))
        }
        func items(_ key: String) -> [String: Item] {
            (root[key] as? [String: [String: Any]] ?? [:]).mapValues { d in
                let decode = (d["decode"] as? [[Any]] ?? []).map { (code: $0[0], word: "\($0[1])") }
                return Item(nodes: d["nodes"] as? [String] ?? [], sql: d["sql"] as? String ?? "", decode: decode)
            }
        }
        dims = items("dimensions")
        flags = items("flags")
        grains = root["grains"] as? [String: String] ?? [:]
        periods = root["periods"] as? [String: String] ?? [:]
        entityFacts = root["entity_facts"] as? [String] ?? []
        listNodes = root["list_nodes"] as? [String] ?? nodes.map { $0[0] }
        notTrackedReasons = Set(root["not_tracked"] as? [String] ?? [])
        let project = root["project"] as? [String: String] ?? [:]
        name = project["name"] ?? "the database"
        guard let prompt = project["system_prompt"] else { throw PlanError("catalog has no project.system_prompt") }
        systemPrompt = prompt
        conninfo = project["conninfo"] ?? ""
        modelFile = project["model_file"] ?? "model.gguf"
        lookup = (root["lookup"] as? [[Any]] ?? []).map {
            (dim: $0[0] as! String, kind: $0[1] as! String, words: $0[2] as? [String] ?? [])
        }
        caseValues = (root["dimensions"] as? [String: [String: Any]] ?? [:]).compactMapValues { $0["values"] as? [String] }
        commonWords = root["common_words"] as? [String] ?? []

        var edges = [String: [(to: String, on: String, optional: Bool)]]()
        for j in root["joins"] as? [[Any]] ?? [] {
            edges[j[0] as! String, default: []].append((to: j[1] as! String, on: j[2] as! String, optional: (j[3] as? Bool) ?? false))
        }
        // Breadth-first over many-to-one edges: the shortest join path from a base to every node.
        func bfs(_ base: String) -> [String: Link] {
            var tree = [base: Link(parent: nil, on: "", optional: false)]
            var queue = [base]
            while !queue.isEmpty {
                let n = queue.removeFirst()
                for e in edges[n] ?? [] where tree[e.to] == nil {
                    tree[e.to] = Link(parent: n, on: e.on, optional: e.optional)
                    queue.append(e.to)
                }
            }
            return tree
        }
        for f in facts { reach[f.name] = bfs(f.base) }
        // Role nodes (a buyer is one role of Person.Person) never start a list.
        for n in listNodes { reach["node:" + n] = bfs(n) }
    }

    func fact(_ name: String) -> Fact {
        if name.hasPrefix("node:") { return Fact(name: name, base: String(name.dropFirst(5)), date: nil, measures: [:]) }
        return facts.first { $0.name == name }!
    }
}

// MARK: compiler

final class PlanCompiler {
    let cat: Catalog
    static let ops: Set<String> = ["=", "<>", ">", "<", ">=", "<=", "in", "not in", "like", "between", "is null", "is not null"]
    static let cmps: Set<String> = ["=", "<>", ">", "<", ">=", "<="]

    init(_ cat: Catalog) { self.cat = cat }

    func compile(json text: String) throws -> String {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8), options: [.fragmentsAllowed]),
              let plan = obj as? [String: Any] else { throw PlanError("not a JSON plan") }
        return try compile(plan)
    }

    /// What a {"not_tracked": "..."} plan says the database does not record, else nil.
    func notTracked(json text: String) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else { return nil }
        return obj["not_tracked"] as? String
    }

    /// alias.column in a SQL fragment, as a whole word: "doc." is not "pdoc.".
    private func mentions(_ text: String, alias: String) -> Bool {
        var from = text.startIndex
        while let r = text.range(of: alias + ".", range: from..<text.endIndex) {
            if r.lowerBound == text.startIndex { return true }
            let c = text[text.index(before: r.lowerBound)]
            if !(c.isLetter || c.isNumber || c == "_") { return true }
            from = r.upperBound
        }
        return false
    }

    func compile(_ plan: [String: Any]) throws -> String {
        if let what = plan["not_tracked"] as? String {
            if !cat.notTrackedReasons.contains(what) { throw PlanError("unknown not_tracked reason '\(what)'") }
            throw PlanError("can't answer from the database: \(what)")
        }
        let measures = try strings(plan["measures"])
        let group = try strings(plan["group_by"])
        let show = try strings(plan["show"])
        let filters = plan["filters"] as? [[String: Any]] ?? []
        let time = plan["time"] as? [String: Any]
        let fname = try chooseFact(plan, measures: measures, group: group, show: show, filters: filters)
        let f = cat.fact(fname)
        for m in measures where f.measures[m] == nil { throw PlanError("\(fname) has no measure '\(m)'") }

        var select = try (group + show).map { try dimExpr($0, f) + " AS " + $0 }
        select += measures.map { "\(f.measures[$0]!) AS \($0)" }
        if select.isEmpty { throw PlanError("plan selects nothing") }

        var whereParts = try filters.map { try filterSQL($0, f) }
        if let t = time, !t.isEmpty { whereParts.append(try timeSQL(t, f)) }
        var having = [String]()
        for h in plan["having"] as? [[String: Any]] ?? [] {
            guard let m = h["measure"] as? String, let sql = f.measures[m] else {
                throw PlanError("\(fname) has no measure '\(h["measure"] ?? "")'")
            }
            let op = h["op"] as? String ?? ">"
            if !Self.cmps.contains(op) { throw PlanError("bad having operator '\(op)'") }
            having.append("\(sql) \(op) \(lit(h["value"] ?? NSNull()))")
        }

        // Joins: every node an expression mentions, plus the path back to the base.
        let usesDate = (time.map { !$0.isEmpty } ?? false) || group.contains { cat.grains[$0] != nil }
        let text = (select + whereParts + having + [usesDate ? (f.date ?? "") : ""]).joined(separator: " ")
        let need = Set(cat.nodeOrder.filter { mentions(text, alias: $0) })
            .union(try neededNodes(group: group, show: show, filters: filters))
        let tree = cat.reach[fname]!
        var joins = [String](), left = Set<String>()
        for n in pathNodes(fname, need).sorted(by: { (depth(tree, $0), $0) < (depth(tree, $1), $1) }) {
            guard let link = tree[n], let parent = link.parent else { continue }
            if link.optional || left.contains(parent) { left.insert(n) }
            joins.append("\(left.contains(n) ? "LEFT JOIN" : "JOIN") \(cat.table[n]!) \(n) ON \(link.on)")
        }

        // A list never repeats a row: "which colors" means each color once.
        var sql = "SELECT \(measures.isEmpty ? "DISTINCT " : "")\(select.joined(separator: ", ")) FROM \(cat.table[f.base]!) \(f.base)"
        if !joins.isEmpty { sql += " " + joins.joined(separator: " ") }
        if !whereParts.isEmpty { sql += " WHERE " + whereParts.joined(separator: " AND ") }
        if !measures.isEmpty && !(group + show).isEmpty {
            sql += " GROUP BY " + (try (group + show).map { try dimExpr($0, f) }).joined(separator: ", ")
        }
        if !having.isEmpty { sql += " HAVING " + having.joined(separator: " AND ") }

        let limit = (plan["limit"] as? NSNumber)?.intValue ?? 0
        var orders: [[String: Any]]
        if let o = plan["order_by"] as? [[String: Any]] {
            orders = o
        } else if limit > 0 && !measures.isEmpty {
            orders = [["measure": measures[0], "dir": "desc"]]
        } else {
            orders = group.filter { cat.grains[$0] != nil }.map { ["dim": $0] as [String: Any] }
        }
        var parts = [String]()
        for o in orders {
            let name = (o["measure"] as? String) ?? (o["dim"] as? String) ?? ""
            if !(measures + group + show).contains(name) { throw PlanError("order_by '\(name)' is not selected") }
            parts.append("\(name) \(((o["dir"] as? String) ?? "asc").lowercased() == "desc" ? "DESC" : "ASC")")
        }
        if !parts.isEmpty { sql += " ORDER BY " + parts.joined(separator: ", ") }
        if limit > 0 { sql += " LIMIT \(limit)" }
        return sql
    }

    // MARK: pieces

    private func strings(_ v: Any?) throws -> [String] {
        guard let v = v else { return [] }
        guard let a = v as? [String] else { throw PlanError("expected a list of names, got \(v)") }
        return a
    }

    func lit(_ v: Any) -> String {
        if let n = v as? NSNumber {
            if CFGetTypeID(n) == CFBooleanGetTypeID() { return n.boolValue ? "TRUE" : "FALSE" }
            if CFNumberIsFloatType(n) {
                let d = n.doubleValue
                return d == d.rounded() && abs(d) < 1e15 ? String(format: "%.1f", d) : "\(d)"
            }
            return "\(n.int64Value)"
        }
        if v is NSNull { return "NULL" }
        return "'" + "\(v)".replacingOccurrences(of: "'", with: "''") + "'"
    }

    private func dimExpr(_ name: String, _ f: Catalog.Fact) throws -> String {
        if let g = cat.grains[name] {
            guard let date = f.date else { throw PlanError("\(f.name) has no date for '\(name)'") }
            return g.replacingOccurrences(of: "{d}", with: date)
        }
        guard let d = cat.dims[name] else { throw PlanError("unknown dimension '\(name)'") }
        if !d.decode.isEmpty {
            let whens = d.decode.map { "WHEN \(lit($0.code)) THEN \(lit($0.word))" }.joined(separator: " ")
            return "CASE \(d.sql) \(whens) END"
        }
        return d.sql
    }

    /// A spoken value ("Mountain") back to the stored code ('M') for decoded dimensions.
    private func filterValue(_ name: String, _ v: Any) -> Any {
        guard let d = cat.dims[name], !d.decode.isEmpty else { return v }
        let s = "\(v)".lowercased()
        return d.decode.first { $0.word.lowercased() == s }?.code ?? v
    }

    private func filterSQL(_ f: [String: Any], _ fact: Catalog.Fact) throws -> String {
        if let flag = f["flag"] as? String {
            guard let fl = cat.flags[flag] else { throw PlanError("unknown flag '\(flag)'") }
            return fl.sql
        }
        guard let name = f["dim"] as? String else { throw PlanError("filter needs a dim or a flag") }
        guard let rawOp = f["op"] as? String else { throw PlanError("filter on '\(name)' needs an op") }
        let op = rawOp.lowercased()
        if !Self.ops.contains(op) { throw PlanError("unknown operator '\(op)'") }
        let col: String
        if cat.grains[name] != nil { col = try dimExpr(name, fact) }
        else if let d = cat.dims[name] { col = d.sql }
        else { throw PlanError("unknown dimension '\(name)'") }
        if op == "is null" || op == "is not null" { return "\(col) \(op.uppercased())" }
        guard let v = f["value"], !(v is NSNull) else { throw PlanError("'\(op)' filter on '\(name)' needs a value") }
        switch op {
        case "in", "not in":
            guard let list = v as? [Any], !list.isEmpty else { throw PlanError("'\(op)' needs a list") }
            return "\(col) \(op.uppercased()) (" + list.map { lit(filterValue(name, $0)) }.joined(separator: ", ") + ")"
        case "between":
            guard let list = v as? [Any], list.count >= 2 else { throw PlanError("'between' needs two values") }
            return "\(col) BETWEEN \(lit(list[0])) AND \(lit(list[1]))"
        case "like":
            return "\(col) ILIKE \(lit("%\(likeText(v))%"))"
        default:
            return "\(col) \(op) \(lit(filterValue(name, v)))"
        }
    }

    private func likeText(_ v: Any) -> String {
        if let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() { return lit(n) }
        return "\(v)"
    }

    private func timeSQL(_ t: [String: Any], _ f: Catalog.Fact) throws -> String {
        guard let date = f.date else { throw PlanError("\(f.name) has no date column for a time filter") }
        let p = t["period"] as? String ?? ""
        guard let tmpl = cat.periods[p] else { throw PlanError("unknown period '\(p)'") }
        let n = (t["n"] as? NSNumber)?.intValue ?? 0
        return tmpl.replacingOccurrences(of: "{d}", with: date)
            .replacingOccurrences(of: "{n}", with: "\(n)")
            .replacingOccurrences(of: "{from}", with: t["from"] as? String ?? "")
            .replacingOccurrences(of: "{to}", with: t["to"] as? String ?? "")
    }

    // MARK: fact choice

    private func neededNodes(group: [String], show: [String], filters: [[String: Any]]) throws -> Set<String> {
        var need = Set<String>()
        for name in group + show where cat.grains[name] == nil {
            guard let d = cat.dims[name] else { throw PlanError("unknown dimension '\(name)'") }
            need.formUnion(d.nodes)
        }
        for f in filters {
            if let flag = f["flag"] as? String {
                guard let fl = cat.flags[flag] else { throw PlanError("unknown flag '\(flag)'") }
                need.formUnion(fl.nodes)
            } else if let name = f["dim"] as? String, let d = cat.dims[name] {
                need.formUnion(d.nodes)
            } else if let name = f["dim"] as? String, cat.grains[name] != nil {
                continue
            } else {
                throw PlanError("unknown filter \(f)")
            }
        }
        return need
    }

    private func chooseFact(_ plan: [String: Any], measures: [String], group: [String], show: [String],
                            filters: [[String: Any]]) throws -> String {
        let need = try neededNodes(group: group, show: show, filters: filters)
        let usesTime = ((plan["time"] as? [String: Any]).map { !$0.isEmpty } ?? false) || group.contains { cat.grains[$0] != nil }
        var cands: [String]
        if let f = plan["fact"] as? String {
            cands = [f]
        } else if !measures.isEmpty {
            cands = cat.facts.filter { f in measures.allSatisfy { f.measures[$0] != nil } }.map { $0.name }
        } else {
            cands = cat.entityFacts + cat.facts.map { $0.name }.filter { !cat.entityFacts.contains($0) }
            if !usesTime { cands += cat.listNodes.map { "node:" + $0 } }
        }
        let ok = cands.filter { c in
            guard let tree = cat.reach[c] else { return false }
            return need.allSatisfy { tree[$0] != nil } && (!usesTime || cat.fact(c).date != nil)
        }
        guard let first = ok.first else {
            throw PlanError("no fact answers measures \(measures) with \(need.sorted())")
        }
        if !measures.isEmpty { return first }
        // For lists, the fact needing the fewest joins (a product list comes from products, not order lines).
        return ok.min { a, b in
            (pathNodes(a, need).count, cands.firstIndex(of: a)!) < (pathNodes(b, need).count, cands.firstIndex(of: b)!)
        }!
    }

    private func pathNodes(_ fact: String, _ need: Set<String>) -> Set<String> {
        let tree = cat.reach[fact]!
        var out = Set<String>()
        for start in need {
            var n: String? = start
            while let cur = n, !out.contains(cur) {
                out.insert(cur)
                n = tree[cur]?.parent
            }
        }
        return out
    }

    private func depth(_ tree: [String: Catalog.Link], _ node: String) -> Int {
        var d = 0, n = node
        while let p = tree[n]?.parent { n = p; d += 1 }
        return d
    }
}

// MARK: value hints

/// Finds database values (category names, territories, products...) in a question, so the prompt
/// can list their exact spellings: "bike sales in canada" -> category=Bikes; territory=Canada.
final class ValueMatcher {
    /// Longest phrase looked up, in words (values.py MAX_WORDS).
    static let maxWords = 8
    /// Postgres version of norm() (values.py NORM_SQL).
    static let normSQL = #"btrim(regexp_replace(regexp_replace(regexp_replace(lower(v::text), '[^0-9a-z]+', ' ', 'g'), '\m([0-9a-z]{2,}[0-9a-rt-z])s\M', '\1', 'g'), ' +', ' ', 'g'))"#

    private let cat: Catalog
    private let db: () throws -> Database?
    private var context = [String: Set<String>]()
    private let common: Set<String>
    /// Each db/context lookup dimension's SELECT of its values (compiled from {"show": [dim]}).
    private var showSQL = [(dim: String, sql: String)]()

    /// `db`: the read-only connection to look values up in (nil: no database values).
    init(_ cat: Catalog, _ compiler: PlanCompiler, db: @escaping () throws -> Database?) {
        self.cat = cat
        self.db = db
        common = Set(cat.commonWords.map(Self.norm))
        for l in cat.lookup {
            context[l.dim] = Set(l.words)
            if l.kind != "decode", let sql = try? compiler.compile(["show": [l.dim]]) {
                showSQL.append((dim: l.dim, sql: sql))
            }
        }
    }

    /// The question's phrases that could be a value: up to maxWords words, 3+ characters, not just digits.
    static func phrases(_ words: [String]) -> [String] {
        var out = [String]()
        guard !words.isEmpty else { return out }
        for n in 1...min(maxWords, words.count) {
            for i in 0...(words.count - n) {
                let k = words[i..<(i + n)].joined(separator: " ")
                if k.count >= 3 && !k.allSatisfy({ $0.isASCII && $0.isNumber }) && !out.contains(k) { out.append(k) }
            }
        }
        return out
    }

    /// One SELECT of (dim, value) for every value of `dims` whose normalized form is one of
    /// `keys` (values.py lookup_sql). Keys are norm() output, only a-z, 0-9 and spaces.
    static func lookupSQL(_ dims: [(dim: String, sql: String)], _ keys: [String]) -> String {
        let arr = "ARRAY[" + keys.map { "'\($0)'" }.joined(separator: ", ") + "]::text[]"
        return dims.map { d in
            "SELECT DISTINCT '\(d.dim)' AS dim, v::text AS value FROM (\(d.sql)) t(v) WHERE \(normSQL) = ANY(\(arr))"
        }.joined(separator: " UNION ALL ")
    }

    /// (dim, value) rows from the database for the question's phrases. A context dimension is
    /// only asked about when the question names it.
    func lookup(_ words: [String]) -> [(dim: String, value: String)] {
        let present = Set(words)
        let dims = showSQL.filter { d in
            guard let c = context[d.dim], !c.isEmpty else { return true }
            return !c.isDisjoint(with: present)
        }
        let keys = Self.phrases(words)
        guard !dims.isEmpty, !keys.isEmpty else { return [] }
        do {
            guard let conn = try db() else { return [] }
            return try conn.query(Self.lookupSQL(dims, keys)).rows.compactMap { r in
                guard r.count == 2, let d = r[0], let v = r[1] else { return nil }
                return (dim: d, value: v)
            }
        } catch {
            FileHandle.standardError.write("dbq: value lookup failed (\(error)); no value hints\n".data(using: .utf8)!)
            return []
        }
    }

    /// Lowercase, punctuation to spaces, plurals folded: "Mountain Bikes" == "mountain bike".
    static func norm(_ s: String) -> String {
        let cleaned = String(s.lowercased().unicodeScalars.map { c -> Character in
            ("a"..."z").contains(c) || ("0"..."9").contains(c) ? Character(c) : " "
        })
        return cleaned.split(separator: " ").map { w -> String in
            w.count > 3 && w.hasSuffix("s") && !w.hasSuffix("ss") ? String(w.dropLast()) : String(w)
        }.joined(separator: " ")
    }

    func match(_ question: String, limit: Int = 6) -> [(dim: String, value: String)] {
        let words = Self.norm(question).split(separator: " ").map(String.init)
        let present = Set(words)
        var found = [String: [String]]()
        for r in lookup(words) { found[r.dim, default: []].append(r.value) }
        // Within a phrase: catalog order, each dimension's values sorted by code point (as Python sorts).
        var index = [String: [(dim: String, value: String)]]()
        for l in cat.lookup {
            let vals: [String]
            if l.kind == "decode" {
                vals = cat.caseValues[l.dim] ?? cat.dims[l.dim]?.decode.map { $0.word } ?? []
            } else {
                vals = (found[l.dim] ?? []).sorted { $0.unicodeScalars.lexicographicallyPrecedes($1.unicodeScalars) }
            }
            for v in vals {
                let key = Self.norm(v)
                if key.count < 3 || key.allSatisfy({ $0.isASCII && $0.isNumber }) || (common.contains(key) && l.dim != "color") {
                    continue
                }
                if !(index[key]?.contains { $0.dim == l.dim && $0.value == v } ?? false) {
                    index[key, default: []].append((dim: l.dim, value: v))
                }
            }
        }
        var spans = [(a: Int, b: Int, found: [(dim: String, value: String)])]()
        if !words.isEmpty {
            for n in stride(from: min(Self.maxWords, words.count), through: 1, by: -1) {
                for i in 0...(words.count - n) {
                    let key = words[i..<(i + n)].joined(separator: " ")
                    if let all = index[key], !spans.contains(where: { $0.a <= i && i + n <= $0.b }) {
                        // Context dimensions only count when the question also names them ("day shift").
                        let hits = all.filter { h in
                            guard let c = context[h.dim], !c.isEmpty else { return true }
                            return !c.isDisjoint(with: present)
                        }
                        if !hits.isEmpty { spans.append((a: i, b: i + n, found: hits)) }
                    }
                }
            }
        }
        spans.sort { ($0.a, $0.b) < ($1.a, $1.b) }
        var hits = [(dim: String, value: String)]()
        for s in spans {
            for h in s.found where !hits.contains(where: { $0.dim == h.dim && $0.value == h.value }) {
                hits.append(h)
            }
        }
        return Array(hits.prefix(limit))
    }

    /// The user message the plan model was trained on: the question, plus the values found in it.
    func prompt(_ question: String) -> String {
        let hits = match(question)
        if hits.isEmpty { return question }
        return question + "\nValues: " + hits.map { "\($0.dim)=\($0.value)" }.joined(separator: "; ")
    }
}
