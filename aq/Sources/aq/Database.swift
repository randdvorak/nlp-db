import CLibpq
import Foundation

struct DatabaseError: Error, CustomStringConvertible {
    let description: String
}

/// A read-only libpq connection: every transaction is READ ONLY and statements time out,
/// so nothing the model writes can change data or hang the terminal.
final class Database {
    private let conn: OpaquePointer

    init(conninfo: String, timeoutMs: Int = 15000) throws {
        let opts = "options='-c default_transaction_read_only=on -c statement_timeout=\(timeoutMs)'"
        guard let c = PQconnectdb("\(conninfo) \(opts)") else { throw DatabaseError(description: "out of memory") }
        conn = c
        guard PQstatus(conn) == CONNECTION_OK else {
            let msg = String(cString: PQerrorMessage(conn)).trimmingCharacters(in: .whitespacesAndNewlines)
            PQfinish(conn)
            throw DatabaseError(description: "could not connect (\(conninfo)): \(msg)")
        }
    }

    deinit { PQfinish(conn) }

    /// Postgres's own check of the query: parse, name resolution and types, without running it.
    func explain(_ sql: String) throws {
        _ = try run("EXPLAIN " + sql)
    }

    func query(_ sql: String) throws -> (columns: [String], rows: [[String?]]) {
        try run(sql)
    }

    private func run(_ sql: String) throws -> (columns: [String], rows: [[String?]]) {
        guard let res = PQexec(conn, sql) else { throw DatabaseError(description: "no result") }
        defer { PQclear(res) }
        switch PQresultStatus(res) {
        case PGRES_TUPLES_OK, PGRES_COMMAND_OK:
            break
        default:
            let msg = String(cString: PQresultErrorMessage(res))
            throw DatabaseError(description: msg.split(separator: "\n").first.map(String.init) ?? msg)
        }
        let nf = PQnfields(res), nt = PQntuples(res)
        let cols = (0..<nf).map { String(cString: PQfname(res, $0)) }
        var rows = [[String?]]()
        rows.reserveCapacity(Int(nt))
        for r in 0..<nt {
            rows.append((0..<nf).map { f in
                PQgetisnull(res, r, f) == 1 ? nil : String(cString: PQgetvalue(res, r, f))
            })
        }
        return (cols, rows)
    }
}
