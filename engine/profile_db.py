"""Draft a project folder for any PostgreSQL database: catalog.yaml and phrasing.yaml.

  python3 profile_db.py --db "dbname=mydb" --out databases/mydb [--schemas public,sales] [--name MyDB]

Reads the system catalogs and a few statistics (row counts, distinct counts, value lengths),
read-only. No data values are written anywhere except enum labels, which are part of the schema.
The result is a draft for a person to review:

  nodes      one alias per table; an extra role alias when a table points at the same table
             twice (film.language_id and film.original_language_id)
  joins      every foreign key, child -> parent (many-to-one); nullable keys are optional.
             A key that is unique in today's data (each film has one film_category row) is
             also joined parent -> child, so the parent reaches what the child points at.
  facts      every table: a row count, SUM and AVG of its numeric columns, its first date column
  dimensions name columns (name, title, first_name + last_name) name the table's entity;
             other text with few distinct values is a dimension, long or near-unique text is
             kind text; numbers kind number; dates kind date; enums list their labels
  flags      boolean columns
  excluded   keys, row-edit stamps, passwords, binary and search columns, with reasons

Then: coverage.py lists anything not handled, compile.py --selftest runs every entry, and
gen_plans.py writes training questions. Review the say: words first; they are how questions
name things, and the draft only has what column names say.
"""
import argparse
import math
import os
import re
import sys

import psycopg2
import yaml

TIME = {
    "grains": {"year": "EXTRACT(YEAR FROM {d})::int", "quarter": "EXTRACT(QUARTER FROM {d})::int",
               "month": "DATE_TRUNC('month', {d})::date", "day": "{d}::date"},
    "periods": {
        "today": "{d} >= CURRENT_DATE AND {d} < CURRENT_DATE + 1",
        "this_month": "{d} >= DATE_TRUNC('month', CURRENT_DATE) AND {d} < CURRENT_DATE + 1",
        "last_month": "{d} >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month' AND {d} < DATE_TRUNC('month', CURRENT_DATE)",
        "this_quarter": "{d} >= DATE_TRUNC('quarter', CURRENT_DATE) AND {d} < CURRENT_DATE + 1",
        "last_quarter": "{d} >= DATE_TRUNC('quarter', CURRENT_DATE) - INTERVAL '3 months' AND {d} < DATE_TRUNC('quarter', CURRENT_DATE)",
        "this_year": "{d} >= DATE_TRUNC('year', CURRENT_DATE) AND {d} < CURRENT_DATE + 1",
        "last_year": "{d} >= DATE_TRUNC('year', CURRENT_DATE) - INTERVAL '1 year' AND {d} < DATE_TRUNC('year', CURRENT_DATE)",
        "past_days": "{d} >= CURRENT_DATE - INTERVAL '{n} days' AND {d} < CURRENT_DATE + 1",
        "past_months": "{d} >= CURRENT_DATE - INTERVAL '{n} months' AND {d} < CURRENT_DATE + 1",
        "year": "{d} >= DATE '{n}-01-01' AND {d} < DATE '{n}-01-01' + INTERVAL '1 year'",
        "between": "{d} >= DATE '{from}' AND {d} < DATE '{to}' + 1",
    },
}
NAME_COLUMNS = ("name", "title", "full_name", "display_name", "label")
STAMP = re.compile(r"^(last_update|last_updated|updated_at|modified|modified_date|modifieddate|rowguid|row_version|xmin)$", re.I)
SECRET = re.compile(r"pass(word)?|salt|secret|token|hash", re.I)
NUMBER_TYPES = ("smallint", "integer", "bigint", "numeric", "real", "double precision", "money")
TEXT_TYPES = ("text", "character varying", "character", "citext", "name")
DATE_TYPES = ("date", "timestamp without time zone", "timestamp with time zone")
PEOPLE_COLUMNS = ("first_name", "firstname", "last_name", "lastname")
PLACE_WORDS = ("city", "country", "state", "province", "region", "district", "address", "territory", "county")
OFF_TOPIC = ["what is the capital of France", "write me a poem", "how do I bake bread", "who won the world cup",
             "what time is it in Tokyo", "tell me a joke"]


def words(name):
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").lower().strip()


def singular(w):
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ses", "xes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def plural(w):
    if w.endswith("y") and w[-2:-1] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("s", "x", "ch", "sh")):
        return w + "es"
    return w + "s"


class Profiler:
    def __init__(self, conninfo, schemas):
        self.conn = psycopg2.connect(conninfo, options="-c default_transaction_read_only=on -c statement_timeout=120000")
        self.conn.autocommit = True
        self.cur = self.conn.cursor()
        self.cur.execute("SELECT word FROM pg_get_keywords() WHERE catcode IN ('R', 'T')")
        self.reserved = {r[0] for r in self.cur.fetchall()}
        if not schemas:
            self.cur.execute("""SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('pg_catalog', 'information_schema')
                                AND nspname NOT LIKE 'pg\\_%' ORDER BY 1""")
            schemas = [r[0] for r in self.cur.fetchall()]
        self.schemas = schemas

    def q(self, sql, *args):
        self.cur.execute(sql, args or None)
        return self.cur.fetchall()

    def ident(self, name):
        return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) and name not in self.reserved else '"' + name.replace('"', '""') + '"'

    def safe(self, name):
        """A catalog name usable as a SQL alias and column alias."""
        name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "x"
        if name[0].isdigit():
            name = "t_" + name
        return name + "_" if name in self.reserved else name

    # ---------------- reading the schema ----------------

    def read(self):
        self.tables = {}  # oid -> {schema, name, cols: [..], pk: [..], rows}
        for oid, schema, name, comment in self.q(
                """SELECT c.oid, n.nspname, c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname = ANY(%s)
                   ORDER BY n.nspname, c.relname""", self.schemas):
            self.tables[oid] = {"schema": schema, "name": name, "comment": comment, "cols": [], "pk": [], "unique": []}
        for oid, t in self.tables.items():
            for attnum, col, typ, base, notnull, gen, comment, enum in self.q(
                    """SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod),
                              format_type(COALESCE(NULLIF(ty.typbasetype, 0), a.atttypid), NULL), a.attnotnull,
                              a.attgenerated <> '', col_description(a.attrelid, a.attnum),
                              (SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e
                               WHERE e.enumtypid = COALESCE(NULLIF(ty.typbasetype, 0), a.atttypid))
                       FROM pg_attribute a JOIN pg_type ty ON ty.oid = a.atttypid
                       WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", oid):
                t["cols"].append({"num": attnum, "name": col, "type": typ, "base": base, "notnull": notnull,
                                  "generated": gen, "comment": comment, "enum": enum})
            for kind, cols in self.q("""SELECT contype, ARRAY(SELECT a.attname FROM unnest(conkey) k
                                         JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = k)
                                         FROM pg_constraint WHERE conrelid = %s AND contype IN ('p', 'u')""", oid):
                if kind == "p":
                    t["pk"] = list(cols)
                else:
                    t["unique"].append(list(cols))
        # Partitions count as their partitioned table (keys declared on each partition included).
        part_of = dict(self.q("""SELECT i.inhrelid, i.inhparent FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
                                 WHERE c.relispartition"""))
        self.fks = []  # (child oid, [cols], parent oid, [cols])
        seen = set()
        for child, ccols, parent, pcols in self.q(
                """SELECT conrelid, ARRAY(SELECT a.attname FROM unnest(conkey) WITH ORDINALITY k(n, i)
                                          JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = k.n ORDER BY k.i),
                          confrelid, ARRAY(SELECT a.attname FROM unnest(confkey) WITH ORDINALITY k(n, i)
                                          JOIN pg_attribute a ON a.attrelid = confrelid AND a.attnum = k.n ORDER BY k.i)
                   FROM pg_constraint WHERE contype = 'f' AND conparentid = 0 ORDER BY conrelid, conname"""):
            child, parent = part_of.get(child, child), part_of.get(parent, parent)
            fk = (child, tuple(ccols), parent, tuple(pcols))
            if child in self.tables and parent in self.tables and fk not in seen:
                seen.add(fk)
                self.fks.append((child, list(ccols), parent, list(pcols)))
        # Short names: the table name, or schema_table when two schemas share it.
        counts = {}
        for t in self.tables.values():
            counts[t["name"]] = counts.get(t["name"], 0) + 1
        for t in self.tables.values():
            t["key"] = self.safe(t["name"] if counts[t["name"]] == 1 else f"{t['schema']}_{t['name']}")
            t["sql"] = f"{self.ident(t['schema'])}.{self.ident(t['name'])}"
            t["rows"] = self.q(f"SELECT count(*) FROM {t['sql']}")[0][0]

    def stats(self, t, col):
        """Distinct values, average and longest text length of a column: counts only, no values."""
        c = self.ident(col)
        return self.q(f"SELECT count(DISTINCT {c}), avg(length({c}::text)), max(length({c}::text)) FROM {t['sql']}")[0]

    def unique_in_data(self, t, cols):
        if [cols] == [t["pk"]] or cols in t["unique"]:
            return True
        cs = ", ".join(self.ident(c) for c in cols)
        n, d = self.q(f"SELECT count(*), count(DISTINCT ({cs})) FROM {t['sql']} WHERE ({cs}) IS NOT NULL")[0]
        return n > 0 and n == d

    # ---------------- drafting the catalog ----------------

    def draft(self, name, conninfo):
        T = self.tables
        nodes, role_nodes, joins, excluded = {}, [], [], {}
        alias_of = {oid: t["key"] for oid, t in T.items()}
        for oid, t in T.items():
            nodes[t["key"]] = t["sql"]
        fk_cols = {}  # oid -> set of columns in a foreign key
        roles = []  # (role alias, table oid, stem) for tables pointed at twice from one table
        for child, ccols, parent, pcols in self.fks:
            fk_cols.setdefault(child, set()).update(ccols)
        by_pair = {}
        for fk in self.fks:
            by_pair.setdefault((fk[0], fk[2]), []).append(fk)
        role_of_fk = {}
        for (child, parent), fks in by_pair.items():
            if len(fks) < 2:
                continue
            for fk in fks:
                stem = self.safe(re.sub(r"_?id$", "", fk[1][0].lower()) or T[parent]["key"])
                if stem == T[parent]["key"]:
                    continue  # the plain one keeps the table's own alias
                alias = stem if stem not in nodes else self.safe(f"{T[child]['key']}_{stem}")
                nodes[alias] = T[parent]["sql"]
                role_nodes.append(alias)
                roles.append((alias, parent, stem))
                role_of_fk[id(fk)] = alias

        def on(ca, ccols, pa, pcols):
            return " AND ".join(f"{ca}.{self.ident(c)} = {pa}.{self.ident(p)}" for c, p in zip(ccols, pcols))

        def nullable(oid, cols):
            return any(not c["notnull"] for c in T[oid]["cols"] if c["name"] in cols)

        one_to_one = []
        for fk in self.fks:
            child, ccols, parent, pcols = fk
            ca, pa = alias_of[child], role_of_fk.get(id(fk), alias_of[parent])
            joins.append([ca, pa, on(ca, ccols, pa, pcols)] + (["optional"] if nullable(child, ccols) else []))
            # A child that extends its parent (the key is part of its primary key, like
            # film_category.film_id) and has at most one row per parent is joined both ways.
            if pa == alias_of[parent] and child != parent and set(ccols) <= set(T[child]["pk"]) \
                    and self.unique_in_data(T[child], ccols):
                one_to_one.append([pa, ca, on(ca, ccols, pa, pcols), "optional"])
        # A role alias reaches what its table reaches (an original language has no parents, a
        # manager's store does).
        for alias, parent, _ in roles:
            for child, ccols, p2, pcols in self.fks:
                if child == parent and p2 != parent:
                    joins.append([alias, alias_of[p2], on(alias, ccols, alias_of[p2], pcols)]
                                 + (["optional"] if nullable(child, ccols) else []))
        have = {(j[0], j[1]) for j in joins}
        joins += [j for j in one_to_one if (j[0], j[1]) not in have]

        dims, flags, facts, lists = {}, {}, {}, {}
        groups = {"people": [], "places": []}
        id_dims = {}  # entity dimensions that are numbers: filtered on made-up ids, never looked up
        names_used = set()

        def unique(n):
            base, i = n, 2
            while n in names_used:
                n, i = f"{base}_{i}", i + 1
            names_used.add(n)
            return n

        excluded["*.last_update"] = "row edit timestamp, no business meaning"
        entity_of = {}  # alias -> entity dimension
        for oid, t in T.items():
            a = t["key"]
            ent = self.safe(singular(t["name"].lower()))
            cols = {c["name"]: c for c in t["cols"]}
            pk = set(t["pk"])
            keys = pk | fk_cols.get(oid, set())
            name_sql = None
            first = next((c for c in cols if c.lower() in ("first_name", "firstname")), None)
            last = next((c for c in cols if c.lower() in ("last_name", "lastname", "surname")), None)
            if first and last:
                name_sql, name_cols = f"{a}.{self.ident(first)} || ' ' || {a}.{self.ident(last)}", {first, last}
                groups["people"].append(ent)
            else:
                col = next((c for c in cols if c.lower() in NAME_COLUMNS or c.lower() in (t["name"].lower(), ent)
                            and cols[c]["base"] in TEXT_TYPES), None)
                if col and cols[col]["base"] in TEXT_TYPES:
                    name_sql, name_cols = f"{a}.{self.ident(col)}", {col}
            if name_sql:
                ent = unique(ent)
                dims[ent] = {"node": a, "sql": name_sql, "say": [words(ent)]}
                entity_of[a] = ent
                if any(w in ent for w in PLACE_WORDS):
                    groups["places"].append(ent)
            elif any(fk[2] == oid for fk in self.fks) and len(t["pk"]) == 1 and t["rows"] <= 1000:
                # A small table others point at, with no name: its id is how people tell them apart ("store 2").
                ent = unique(ent)
                dims[ent] = {"node": a, "sql": f"{a}.{self.ident(t['pk'][0])}", "say": [words(ent)]}
                entity_of[a] = ent
                id_dims[ent] = [f"for {words(ent)} {{v}}", f"at {words(ent)} {{v}}"]
                name_cols = set()
            else:
                name_cols = set()
            attrs = []
            for c in t["cols"]:
                n, base = c["name"], c["base"]
                full = f"{t['schema']}.{t['name']}.{n}"
                if n in name_cols:
                    continue
                if n in keys and base not in DATE_TYPES:
                    if n not in {x for fk in self.fks for x in (fk[1] if fk[0] == oid else fk[3] if fk[2] == oid else [])}:
                        excluded[full] = "key with no business meaning"
                    continue
                if STAMP.match(n):
                    excluded.setdefault(f"*.{n}", "row edit stamp, no business meaning")
                    continue
                if SECRET.search(n):
                    excluded[full] = "secret (security)"
                    continue
                if base in ("bytea", "tsvector", "json", "jsonb", "uuid", "xml") or base.endswith("[]") or c["generated"]:
                    excluded[full] = f"{base}{' generated' if c['generated'] else ''}: not something questions filter or group by"
                    continue
                stem = words(n)
                dname = unique(self.safe(n if n.lower().startswith(ent) else f"{ent}_{n}"))
                say = list(dict.fromkeys([f"{words(ent)} {stem}" if not stem.startswith(words(ent)) else stem, stem]))
                item = {"node": a, "sql": f"{a}.{self.ident(n)}", "say": say}
                if c["comment"]:
                    item["describe"] = c["comment"]
                if base == "boolean":
                    names_used.discard(dname)
                    stem = re.sub(r"^(is|has)_|bool$|_flag$|flag$", "", n.lower()) or n.lower()
                    fname = unique(self.safe(f"{ent}_{stem}"))
                    flags[fname] = {"node": a, "sql": item["sql"], "say": [f"{stem} {words(plural(ent))}", words(stem)]}
                    continue
                if c["enum"]:
                    item["values"] = list(c["enum"])
                elif base in NUMBER_TYPES:
                    item["kind"] = "number"
                elif base in DATE_TYPES:
                    item["kind"] = "date"
                elif base in TEXT_TYPES:
                    distinct, avg_len, max_len = self.stats(t, n)
                    if (avg_len or 0) > 60:
                        item["kind"] = "text"
                    elif distinct > 50 and distinct > 0.2 * max(t["rows"], 1):
                        item["kind"] = "text"  # near-unique (emails, phones): shown in lists, not looked up
                else:
                    item["kind"] = "text"
                dims[dname] = item
                attrs.append(dname)
                if any(w in n.lower() for w in PLACE_WORDS) and "kind" not in item:
                    groups["places"].append(dname)
            # Facts: one per table.
            fname = unique(self.safe(plural(singular(t["name"].lower()))))
            measures = {unique(self.safe(f"{singular(t['name'].lower())}_count")):
                        {"sql": "COUNT(*)", "say": [f"number of {words(plural(singular(t['name'])))}", f"how many {words(plural(singular(t['name'])))}"]}}
            for c in t["cols"]:
                if c["base"] in NUMBER_TYPES and c["name"] not in keys and not c["generated"] and not STAMP.match(c["name"]) \
                        and not re.search(r"year|number|code|_no$", c["name"], re.I):
                    for agg, word in (("SUM", "total"), ("AVG", "average")):
                        m = unique(self.safe(f"{'total' if agg == 'SUM' else 'avg'}_{c['name']}"))
                        measures[m] = {"sql": f"{agg}({a}.{self.ident(c['name'])})", "say": [f"{word} {words(c['name'])}"]}
            date = next((c for c in t["cols"] if c["base"] in DATE_TYPES and not STAMP.match(c["name"])), None)
            fact = {"base": a}
            if date:
                fact["date"] = f"{a}.{self.ident(date['name'])}"
            fact["describe"] = t["comment"] or f"one row per {words(singular(t['name']))}"
            if a in entity_of and attrs:
                fact["list"] = {"entity": entity_of[a], "attrs": attrs[:16]}
            fact["measures"] = measures
            facts[fname] = fact
            facts[fname]["_rows"] = t["rows"]
        # Role aliases get their table's entity name ("original language").
        for alias, parent, stem in roles:
            src = entity_of.get(alias_of[parent])
            if src:
                d = unique(alias)
                dims[d] = {"node": alias, "sql": dims[src]["sql"].replace(f"{alias_of[parent]}.", f"{alias}."), "say": [words(alias)]}
        rows = {f: x.pop("_rows") for f, x in facts.items()}
        catalog = {
            "project": {"name": name, "conninfo": conninfo, "model_file": f"{self.safe(name)}-plans.gguf",
                        "schemas": self.schemas},
            "lookup": {"skip": list(id_dims), "context": [], "common_words": []},
            "nodes": nodes, "role_nodes": role_nodes, "joins": joins, "facts": facts,
            "dimensions": dims, "flags": flags, "time": TIME,
            "not_tracked": [f"the question is not about the {name} data"],
            "excluded": excluded,
        }
        phrasing = {
            "fact_weights": {f: round(1 + math.log10(max(n, 1)) / 2, 2) for f, n in rows.items()},
            "id_dims": id_dims,
            "not_tracked_questions": [{"reason": catalog["not_tracked"][0], "questions": OFF_TOPIC}],
            "made_up_values": {
                "groups": {g: v for g, v in groups.items() if v},
                "shapes": {"people": "f'{word(pick([1, 2]))} {word(pick([2, 3]))}'",
                           "places": "word(pick([2, 3])) + pick(['', '', 'ia', ' Falls', ' Coast', 'land', ' Bay'])"},
                "siblings": [[g] for g, v in groups.items() if len(v) > 1],
            },
        }
        return catalog, phrasing


HEADER = """# Drafted by engine/profile_db.py from {conninfo}. Review before training:
#  - say: the words people use for each fact, measure, dimension and flag (the draft only has
#    what the column names say)
#  - facts: drop the ones nobody asks about; name money measures the way people do ("revenue")
#  - dimensions: kind text are shown in lists only; lookup values come from the database live
#  - joins marked "one-to-one in today's data" assume each parent row has at most one child row
#  - lookup.context: dimensions whose values are everyday words ("Day" shift), matched only
#    next to a word naming them; lookup.skip: never matched
# python3 coverage.py --project {out} lists columns that are neither used nor excluded.
"""


def dump(obj):
    class D(yaml.SafeDumper):
        pass

    def seq(d, data):
        flow = all(not isinstance(x, (list, dict)) for x in data) and len(str(data)) < 110
        return d.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)
    D.add_representer(list, seq)
    return yaml.dump(obj, Dumper=D, sort_keys=False, allow_unicode=True, width=10 ** 6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="libpq conninfo, e.g. dbname=mydb")
    ap.add_argument("--out", required=True, help="project folder to create, e.g. databases/mydb")
    ap.add_argument("--schemas", help="comma-separated (default: all but the system schemas)")
    ap.add_argument("--name", help="what the database is called in questions (default: the folder name)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing catalog.yaml")
    args = ap.parse_args()
    conninfo = args.db if "=" in args.db else f"dbname={args.db}"
    name = args.name or os.path.basename(os.path.abspath(args.out))
    cat_path = os.path.join(args.out, "catalog.yaml")
    if os.path.exists(cat_path) and not args.force:
        sys.exit(f"{cat_path} exists; pass --force to overwrite")
    p = Profiler(conninfo, args.schemas.split(",") if args.schemas else None)
    p.read()
    catalog, phrasing = p.draft(name, conninfo)
    os.makedirs(args.out, exist_ok=True)
    with open(cat_path, "w", encoding="utf-8") as f:
        f.write(HEADER.format(conninfo=conninfo, out=args.out) + "\n" + dump(catalog))
    with open(os.path.join(args.out, "phrasing.yaml"), "w", encoding="utf-8") as f:
        f.write("# Drafted by engine/profile_db.py; see databases/adventureworks/phrasing.yaml for every key.\n\n" + dump(phrasing))
    print(f"{len(p.tables)} tables -> {len(catalog['facts'])} facts, {len(catalog['dimensions'])} dimensions, "
          f"{len(catalog['flags'])} flags, {len(catalog['joins'])} joins, {len(catalog['excluded'])} exclusions in {args.out}")


if __name__ == "__main__":
    main()
