"""Value lookup: find database values (a territory, a product, a person) named in a question.

Nothing is stored. For each question, one read-only query asks the database which of the
question's phrases are values of a lookup dimension, and the matches become hints:

  python3 values.py --project DIR "bike sales in canada" [--db CONNINFO]
  # -> bike sales in canada\nValues: category=Bikes; territory=Canada; ...

The hints go into the model's prompt, so the model copies real spellings instead of knowing
them. Training uses made-up values (see gen_plans.py), so the model learns to copy, not the
data. dbq does the same in Swift (ValueMatcher), with the same query.

Three kinds of lookup dimension, in catalog order (lookup_dims):
  db       values from the database, matched anywhere in the question
  context  values from the database that are often everyday words ("Day" shift, "Home"
           address): looked up only when the question also names the dimension ("day shift")
  decode   words from the catalog's own code meanings (product line "Mountain"), no query
"""
import re
import sys

import compile as C

LOOKUP_SETTINGS = C.CAT.get("lookup", {})
# Never matched: their values are the catalog's own words that the model learns from the
# wording itself ("high", "single", "online").
NO_MATCH = set(LOOKUP_SETTINGS.get("skip", []))
# Matched only next to a word naming the dimension ("day shift", "home address").
CONTEXT = set(LOOKUP_SETTINGS.get("context", []))
# Longest phrase looked up, in words ("Mountain-100 Black, 38" is 4).
MAX_WORDS = 8
# Postgres version of norm(): lowercase, anything but a-z0-9 to spaces, words over 3 letters
# ending in one "s" lose it, spaces collapsed.
NORM_SQL = (r"btrim(regexp_replace(regexp_replace(regexp_replace(lower({x}), '[^0-9a-z]+', ' ', 'g'), "
            r"'\m([0-9a-z]{{2,}}[0-9a-rt-z])s\M', '\1', 'g'), ' +', ' ', 'g'))")
# Value names that are also everyday words in questions ("sales by territory").
COMMON = set(LOOKUP_SETTINGS.get("common_words", []))
STOP = {"of", "the", "a", "an", "to", "by", "for", "in", "on", "at", "or", "and", "how", "what", "who", "is"}


def _singular(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def norm(s):
    """Lowercase, punctuation to spaces, plurals folded: "Mountain Bikes" == "mountain bike"."""
    return " ".join(_singular(w) for w in re.sub(r"[^0-9a-z]+", " ", s.lower()).split())


def lookup_dims():
    """[(dim, kind, context words)] in catalog order."""
    out = []
    for name, d in C.CAT["dimensions"].items():
        if name in NO_MATCH or d.get("kind") in ("number", "date", "text"):
            continue
        if "decode" in d or "values" in d:  # the catalog's own code meanings or CASE values
            out.append((name, "decode", []))
        else:
            words = sorted({w for s in d["say"] for w in norm(s).split() if w not in STOP}) if name in CONTEXT else []
            out.append((name, "context" if name in CONTEXT else "db", words))
    return out


def catalog_words(name):
    d = C.CAT["dimensions"][name]
    return [str(v) for v in d["decode"].values()] if "decode" in d else [str(v) for v in d["values"]]


def phrases(words):
    """The question's phrases that could be a value: up to MAX_WORDS words, at least 3
    characters, not just digits (shorter or numeric values are never hints)."""
    out = []
    for n in range(1, min(MAX_WORDS, len(words)) + 1):
        for i in range(len(words) - n + 1):
            k = " ".join(words[i:i + n])
            if len(k) >= 3 and not k.isdigit() and k not in out:
                out.append(k)
    return out


def lookup_sql(dims, keys):
    """One SELECT: (dim, value) for every value of `dims` whose normalized form is one of `keys`.
    keys are norm() output (only a-z, 0-9 and spaces), so they are safe as literals."""
    arr = "ARRAY[" + ", ".join(f"'{k}'" for k in keys) + "]::text[]"
    return " UNION ALL ".join(
        f"SELECT DISTINCT '{d}' AS dim, v::text AS value FROM ({C.compile_plan({'show': [d]})}) t(v) "
        f"WHERE {NORM_SQL.format(x='v::text')} = ANY({arr})" for d in dims)


class Matcher:
    """`db`: a read-only connection (compile.connect) to look values up in, or None for no
    database values (training, where the hints are made up)."""

    def __init__(self, db=None):
        self.cur = db.cursor() if db is not None else None
        self.context = {}
        self.dims = []
        self.common = {norm(w) for w in COMMON}
        for name, kind, words in lookup_dims():
            self.context[name] = set(words)
            if kind != "decode":
                self.dims.append(name)

    def _add(self, index, dim, v):
        key = norm(v)
        if len(key) < 3 or key.isdigit() or (key in self.common and dim != "color"):
            return
        if (dim, v) not in index.setdefault(key, []):
            index[key].append((dim, v))

    def lookup(self, words):
        """(dim, value) rows from the database for the question's phrases. A context dimension
        is only asked about when the question names it."""
        present = set(words)
        dims = [d for d in self.dims if not self.context[d] or self.context[d] & present]
        keys = phrases(words)
        if self.cur is None or not dims or not keys:
            return []
        self.cur.execute(lookup_sql(dims, keys))
        return self.cur.fetchall()

    def match(self, question, limit=6, extra=()):
        """Values named in the question, longest phrase first. `extra` adds (dim, value) pairs
        for this call only (training uses it for made-up values)."""
        words = norm(question).split()
        present = set(words)
        # Within a phrase: catalog order, each dimension's values sorted, made-up ones included
        # (so training lists them the way the live lookup does).
        found = {}
        for d, v in list(self.lookup(words)) + list(extra):
            found.setdefault(d, []).append(v)
        index = {}
        for name, kind, _ in lookup_dims():
            for v in catalog_words(name) if kind == "decode" else sorted(set(found.get(name, []))):
                self._add(index, name, v)
        spans = []
        for n in range(min(MAX_WORDS, len(words)), 0, -1):
            for i in range(len(words) - n + 1):
                key = " ".join(words[i:i + n])
                if key in index and not any(a <= i and i + n <= b for a, b, _ in spans):
                    hits = [(d, v) for d, v in index[key] if not self.context.get(d) or self.context[d] & present]
                    if hits:
                        spans.append((i, i + n, hits))
        spans.sort()
        hits = []
        for _, _, found in spans:
            for dim, v in found:
                if (dim, v) not in hits:
                    hits.append((dim, v))
        return hits[:limit]


def prompt(question, matcher, extra=()):
    """The user message: the question, plus the values found in it."""
    hits = matcher.match(question, extra=extra)
    if not hits:
        return question
    return question + "\nValues: " + "; ".join(f"{d}={v}" for d, v in hits)


if __name__ == "__main__":
    args = sys.argv[1:]
    db = C.P.CONNINFO
    if "--db" in args:
        i = args.index("--db")
        db = args[i + 1]
        del args[i:i + 2]
    print(prompt(" ".join(args), Matcher(C.connect(db))))
