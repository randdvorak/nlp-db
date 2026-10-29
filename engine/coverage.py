"""Check that the catalog covers every table and column in the database.

  python3 coverage.py --project databases/<name> [--db CONNINFO]

A column counts as covered when catalog SQL uses it: through an alias of its table
(alias.Column in a join, measure, dimension, flag or fact date), or inside a subquery
node that reads its table. Columns listed under `excluded:` in catalog.yaml are left
out on purpose, with the reason. The schemas checked are the project's `schemas:` setting, or
every schema except the system ones. Exit 1 if anything is neither covered nor excluded.
"""
import re
import sys

import compile as C



def catalog_sql():
    """(alias, sql) for every SQL fragment in the catalog."""
    out = []
    for e in C.CAT["joins"]:
        out.append(e[2])
    for f in C.CAT["facts"].values():
        out.append(f.get("date") or "")
        out += [m["sql"] for m in f["measures"].values()]
    out += [d["sql"] for d in C.CAT["dimensions"].values()]
    out += [d["sql"] for d in C.CAT["flags"].values()]
    return " ".join(out)


def main(db):
    cur = C.connect(db).cursor()
    schemas = C.P.SETTINGS.get("schemas")
    if not schemas:
        cur.execute("""SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('pg_catalog', 'information_schema')
                       AND nspname NOT LIKE 'pg\\_%'""")
        schemas = [r[0] for r in cur.fetchall()]
    cur.execute("""SELECT c.table_schema, c.table_name, c.column_name FROM information_schema.columns c
                   JOIN information_schema.tables t USING (table_schema, table_name)
                   JOIN pg_class pc ON pc.oid = format('%%I.%%I', c.table_schema, c.table_name)::regclass
                   WHERE t.table_type = 'BASE TABLE' AND NOT pc.relispartition AND c.table_schema IN %s
                   ORDER BY 1, 2, c.ordinal_position""", (tuple(schemas),))
    cols = cur.fetchall()
    text = catalog_sql()
    excluded = {k.lower(): v for k, v in C.CAT.get("excluded", {}).items()}
    tables_by_alias, subqueries = {}, []
    for alias, t in C.CAT["nodes"].items():
        if t.strip().startswith("("):
            subqueries.append(t)
        else:
            tables_by_alias.setdefault(t.lower(), []).append(alias)
    used = set()  # (table, column) lowercase
    for table, aliases in tables_by_alias.items():
        for a in aliases:
            for col in re.findall(rf"(?<![A-Za-z0-9_]){a}\.\"?([A-Za-z_][A-Za-z0-9_]*)", text):
                used.add((table, col.lower()))
    for sq in subqueries:
        words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sq)}
        for t in re.findall(r"(?:FROM|JOIN)\s+([A-Za-z]+\.[A-Za-z]+)", sq):
            for s, tn, c in cols:
                if f"{s}.{tn}" == t.lower() and c.lower() in words:
                    used.add((t.lower(), c.lower()))
    tables = {f"{s}.{t}" for s, t, _ in cols}
    touched = {t for t, _ in used} | set(tables_by_alias)
    missing, n_used, n_excl = [], 0, 0
    for s, t, c in cols:
        table = f"{s}.{t}"
        if (table, c.lower()) in used:
            n_used += 1
        elif table in excluded or f"{table}.{c}".lower() in excluded or f"*.{c}".lower() in excluded:
            n_excl += 1
        else:
            missing.append(f"{table}.{c}")
    untouched = sorted(t for t in tables if t not in touched and t not in excluded)
    print(f"{len(tables)} tables, {len(cols)} columns: {n_used} covered by the catalog, {n_excl} excluded on purpose, "
          f"{len(missing)} not covered")
    for m in missing:
        print("  NOT COVERED", m)
    for t in untouched:
        print("  TABLE NOT USED", t)
    return not missing and not untouched


if __name__ == "__main__":
    db = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else None
    sys.exit(0 if main(db) else 1)
