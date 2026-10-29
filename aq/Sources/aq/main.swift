import CLlama
import Foundation

// MARK: data

let env = ProcessInfo.processInfo.environment
let dataDir = env["AQ_HOME"].map { URL(fileURLWithPath: $0) }
    ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/aq")

func dataFile(_ name: String, builtIn: String) -> String {
    (try? String(contentsOf: dataDir.appendingPathComponent(name), encoding: .utf8)) ?? builtIn
}

let catalog: Catalog = {
    do { return try Catalog(json: dataFile("catalog.json", builtIn: EmbeddedData.catalog)) } catch {
        FileHandle.standardError.write("aq: catalog: \(error)\n".data(using: .utf8)!)
        exit(2)
    }
}()

let usage = """
usage: aq [options] "question about \(catalog.name)"

Turns a question into a plan over the \(catalog.name) semantic catalog with the fine-tuned
model, compiles the plan to one PostgreSQL SELECT, runs it on a read-only connection and
prints the rows.

options:
  -n, --query-only     print the SQL only, don't run it
  -p, --show-plan      print the plan to stderr
  --compile PLAN       compile a plan you wrote (JSON) and run it, no model
  --check-file F.jsonl compile the gold plan of each line and print {sql, prompt} (testing)
  --print-grammar      print the plan grammar aq builds from its catalog (testing)
  -q, --show-query     print the SQL to stderr before the results
  --json               rows as JSON: {query, forced, columns, rows, plan}, or {not_tracked, ...}
  --csv                rows as CSV
  --limit N            print at most N rows (default 50 in table output, all otherwise)
  --db CONNINFO        libpq connection string (default: $AQ_DB, else "\(catalog.conninfo)");
                       relative dates like "last month" need data that reaches today
  --grammar            always generate under the grammar (skip the free first attempt)
  --model PATH         model file (default: $AQ_MODEL, else <data dir>/\(catalog.modelFile))
  --eval FILE.jsonl    translate every question in a dataset file and print JSON lines (testing)
  -v, --verbose        show llama.cpp logs and why a free attempt was rejected
  --version            print the version
  -h, --help

data dir: $AQ_HOME, else ~/Library/Application Support/aq
  \(catalog.modelFile)   the plan model
  catalog.json         optional override of the built-in catalog (the grammar is built from it)

exit codes: 0 ok (with or without rows), 1 no valid query, 2 setup, usage or database error,
            3 the question can't be answered from the database (e.g. full-time status)
"""

let version = "aq 0.8.0"
// The prompt the model was trained with (engine/project.py), carried in the catalog.
let planSystemPrompt = catalog.systemPrompt

var generator: Generator? = nil

/// Free the model before exiting: ggml's Metal backend asserts at exit if buffers are still allocated.
func finish(_ code: Int32) -> Never {
    generator = nil
    llama_backend_free()
    exit(code)
}

func fail(_ msg: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(("aq: " + msg + "\n").data(using: .utf8)!)
    finish(code)
}

func note(_ msg: String) {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
}

// MARK: arguments

var showPlan = false, planArg: String? = nil, checkFile: String? = nil
var queryOnly = false, showQuery = false, json = false, csv = false, alwaysGrammar = false, verbose = false
var limit: Int? = nil
var conninfo = ProcessInfo.processInfo.environment["AQ_DB"] ?? catalog.conninfo
var modelArg: String? = nil, evalFile: String? = nil, printGrammar = false
var words = [String]()

var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let a = args.removeFirst()
    func value() -> String {
        guard !args.isEmpty else { fail("\(a) needs a value\n\n" + usage) }
        return args.removeFirst()
    }
    switch a {
    case "-n", "--query-only": queryOnly = true
    case "-q", "--show-query": showQuery = true
    case "-p", "--show-plan": showPlan = true
    case "--compile": planArg = value()
    case "--check-file": checkFile = value()
    case "--json": json = true
    case "--csv": csv = true
    case "--limit": guard let n = Int(value()), n > 0 else { fail("--limit needs a positive number") }; limit = n
    case "--db": conninfo = value()
    case "--grammar": alwaysGrammar = true
    case "--print-grammar": printGrammar = true
    case "--model": modelArg = value()
    case "--eval": evalFile = value()
    case "-v", "--verbose": verbose = true
    case "--version": print(version); exit(0)
    case "-h", "--help": print(usage); exit(0)
    default:
        if a.hasPrefix("-") && a.count > 1 { fail("unknown option \(a)\n\n" + usage) }
        words.append(a)
    }
}

let planCompiler = PlanCompiler(catalog)
/// Only plans built from catalog names fit; generation under it can't invent a name.
let planGrammar = PlanGrammar.build(catalog)
/// The plan grammar without the not_tracked answer: for the retry after a made-up reason.
let planGrammarNoNotTracked = planGrammar.split(separator: "\n", omittingEmptySubsequences: false).map { line in
    line.hasPrefix("root ::= ") ? "root ::= \"{\" item (\",\" item)* \"}\"" : String(line)
}.joined(separator: "\n")

func loadModel() -> Generator {
    if let g = generator { return g }
    let path = modelArg ?? env["AQ_MODEL"] ?? dataDir.appendingPathComponent(catalog.modelFile).path
    guard FileManager.default.fileExists(atPath: path) else {
        fail("no model at \(path): copy the plan model there, or pass --model / set AQ_MODEL")
    }
    do { generator = try Generator(modelPath: path, verbose: verbose) } catch { fail("\(error)") }
    return generator!
}

var db: Database? = nil
func database() -> Database {
    if let d = db { return d }
    do { db = try Database(conninfo: conninfo) } catch { fail("\(error)") }
    return db!
}

/// Value hints are looked up in the database for each question (read-only, nothing stored).
/// Without a database the question gets no hints.
let matcher = ValueMatcher(catalog, planCompiler, db: {
    if db == nil { db = try? Database(conninfo: conninfo) }
    return db
})

/// The JSON object in the model's reply (drops code fences and anything around the braces).
func cleanPlan(_ text: String) -> String {
    guard let a = text.firstIndex(of: "{"), let b = text.lastIndex(of: "}"), a < b else {
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return String(text[a...b])
}

/// Free attempt, compile (and EXPLAIN), grammar retry.
/// Returns the plan, its SQL, and whether the grammar was needed.
func translatePlan(_ question: String, explain: Bool) -> (plan: String, sql: String, forced: Bool)? {
    let gen = loadModel()
    let messages = [("system", planSystemPrompt), ("user", matcher.prompt(question))]
    var madeUpReason = false
    func attempt(_ g: String?) throws -> (String, String) {
        let plan = cleanPlan(try gen.generate(messages: messages, grammar: g, maxTokens: 250))
        // {"not_tracked": "..."}: a valid answer with no query, but only with one of the
        // catalog's fixed reasons. A made-up reason usually means the model missed a real plan.
        if let what = planCompiler.notTracked(json: plan) {
            if planCompiler.cat.notTrackedReasons.contains(what) { return (plan, "") }
            madeUpReason = true
            throw PlanError("made-up not_tracked reason '\(what)'")
        }
        let sql = try planCompiler.compile(json: plan)
        if explain { try database().explain(sql) }
        return (plan, sql)
    }
    if !alwaysGrammar {
        do {
            let (p, q) = try attempt(nil)
            return (p, q, false)
        } catch {
            if verbose { note("free attempt rejected: \(error)") }
        }
    }
    do {
        // After a made-up reason, the retry must be a real plan.
        let (p, q) = try attempt(madeUpReason ? planGrammarNoNotTracked : planGrammar)
        return (p, q, true)
    } catch {
        if verbose { note("grammar attempt rejected: \(error)") }
        return nil
    }
}

// MARK: output

func csvField(_ v: String?) -> String {
    guard let v = v else { return "" }
    return v.contains(where: { ",\"\n".contains($0) }) ? "\"" + v.replacingOccurrences(of: "\"", with: "\"\"") + "\"" : v
}

func printTable(_ cols: [String], _ rows: [[String?]], limit: Int) {
    let shown = Array(rows.prefix(limit))
    var width = cols.map { $0.count }
    for r in shown { for (i, v) in r.enumerated() { width[i] = min(max(width[i], (v ?? "NULL").count), 40) } }
    func line(_ vals: [String]) -> String {
        zip(vals, width).map { v, w in
            let s = v.count > w ? String(v.prefix(w - 1)) + "…" : v
            return s.padding(toLength: w, withPad: " ", startingAt: 0)
        }.joined(separator: " | ").trimmingCharacters(in: .whitespaces)
    }
    print(line(cols))
    print(width.map { String(repeating: "-", count: $0) }.joined(separator: "-+-"))
    for r in shown { print(line(r.map { $0 ?? "NULL" })) }
    print("(\(rows.count) row\(rows.count == 1 ? "" : "s")\(rows.count > shown.count ? ", \(shown.count) shown" : ""))")
}

func jsonString(_ obj: Any) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]), encoding: .utf8)!
}

// MARK: test modes

if printGrammar { print(planGrammar, terminator: ""); finish(0) }

/// The user question of a dataset line, without a "Values:" line a dataset may already carry.
func questionOf(_ obj: [String: Any]) -> String? {
    guard let msgs = obj["messages"] as? [[String: String]],
          let user = msgs.first(where: { $0["role"] == "user" })?["content"] else { return nil }
    return user.components(separatedBy: "\nValues: ").first
}

if let path = checkFile {
    // Parity check with compile.py / values.py: SQL of each gold plan and the prompt with hints.
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { fail("cannot read \(path)") }
    for line in text.split(separator: "\n") {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
              let msgs = obj["messages"] as? [[String: String]], let q = questionOf(obj),
              let plan = msgs.first(where: { $0["role"] == "assistant" })?["content"] else { continue }
        var out: [String: Any] = ["prompt": matcher.prompt(q)]
        if let what = planCompiler.notTracked(json: plan) { out["not_tracked"] = what }
        else { do { out["sql"] = try planCompiler.compile(json: plan) } catch { out["error"] = "\(error)" } }
        print(jsonString(out))
    }
    finish(0)
}

if let path = evalFile {
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { fail("cannot read \(path)") }
    for line in text.split(separator: "\n") {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
              let question = questionOf(obj) else { continue }
        let r = translatePlan(question, explain: false)
        print(jsonString(["text": r?.plan ?? "", "sql": r?.sql ?? "", "forced": r?.forced ?? false, "valid": r != nil]))
    }
    finish(0)
}

// MARK: one question

let sql: String
var forced = false
var planText: String? = nil
if let p = planArg {
    do { sql = try planCompiler.compile(json: p) } catch { fail("plan rejected: \(error)", code: 1) }
    planText = p
} else {
    let question = words.joined(separator: " ").trimmingCharacters(in: .whitespaces)
    if question.isEmpty { print(usage); finish(2) }
    guard let r = translatePlan(question, explain: !queryOnly) else {
        fail("could not write a valid plan for that question", code: 1)
    }
    (planText, sql, forced) = (r.plan, r.sql, r.forced)
    if let what = planCompiler.notTracked(json: r.plan) {
        if showPlan { note(r.plan) }
        if forced { note("aq: grammar-forced, please check: the first attempt was not valid") }
        if json { print(jsonString(["not_tracked": what, "forced": forced, "plan": ["not_tracked": what]])) }
        else { print("Can't answer from the database: \(what).") }
        finish(3)
    }
}
if showPlan, let p = planText { note(p) }
if forced { note("aq: grammar-forced, please check: the first attempt was not valid") }

if queryOnly { print(sql); finish(0) }
if showQuery { note(sql) }

let result: (columns: [String], rows: [[String?]])
do { result = try database().query(sql) } catch { fail("\(error)") }

if json {
    let rows = result.rows.prefix(limit ?? Int.max).map { $0.map { $0 as Any? ?? NSNull() } }
    var out: [String: Any] = ["query": sql, "forced": forced, "columns": result.columns, "rows": Array(rows)]
    if let p = planText, let obj = try? JSONSerialization.jsonObject(with: Data(p.utf8)) { out["plan"] = obj }
    print(jsonString(out))
} else if csv {
    print(result.columns.map { csvField($0) }.joined(separator: ","))
    for r in result.rows.prefix(limit ?? Int.max) { print(r.map(csvField).joined(separator: ",")) }
} else {
    printTable(result.columns, result.rows, limit: limit ?? 50)
}
finish(0)
