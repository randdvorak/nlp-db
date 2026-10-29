# aq: ask a PostgreSQL database questions in English

```
aq "how many vendors are in California"
aq -n "sales by territory last year"      # print the SQL only
```

aq turns a question into a small JSON **plan** with a fine-tuned model (Qwen2.5-Coder-1.5B,
run locally through llama.cpp), compiles the plan to **one read-only SELECT**, and runs it.
It never writes: every connection is read-only, with a statement timeout.

The model only knows the catalog's names: measures, dimensions, flags and periods. It never
sees the database's own values. When a question names one ("Canada", "Ken Sánchez"), aq looks
it up in the database at question time and gives the model a hint line
(`Values: territory=Canada`); training uses made-up values, so the model learns to copy hints
rather than memorize data.

## Layout

```
engine/              generic Python: profile, compile, value lookup, training data, scoring
aq/                  the Swift command line tool (macOS, Apple silicon)
training/            MLX LoRA settings and a NaN-guarded trainer
scripts/             prepare.sh, train.sh, build-aq.sh, evaluate.sh
databases/<name>/    everything about one database:
  catalog.yaml         tables, joins, facts and measures, dimensions, flags, time, settings
  phrasing.yaml        how people ask (question wording, made-up value shapes, weights)
  tests.py             optional hand-written questions with gold plans, for scoring
  data/, work/         generated (not in git)
```

`databases/adventureworks` is the worked example (155 test questions; the round 5 model gets
131 right). `databases/pagila` is an unreviewed draft made by `profile_db.py` from the Pagila
sample database, to show what a first draft looks like.

## Setup

macOS on Apple silicon, Xcode (its `/usr/bin/swift`), Homebrew `libpq`, `cmake` and
`llama.cpp` (for `llama-quantize`), and Python 3 with `pip install -r requirements.txt`.
Training also needs a Python with `mlx==0.31.2` and `mlx-lm` (mlx 0.32 went NaN for us),
and one with llama.cpp's `requirements/requirements-convert_hf_to_gguf.txt`.

## Pointing it at a new database

1. **Draft the catalog** (read-only):
   ```
   python3 engine/profile_db.py --db "dbname=mydb" --out databases/mydb --name "My Company"
   ```
   It writes `catalog.yaml` and `phrasing.yaml`: a node per table, a join per foreign key, a
   fact per table (row count, sums and averages), dimensions from name and text columns,
   flags from booleans, and exclusions with reasons.
2. **Review it.** This is where the quality comes from. Most of all, the `say:` words: the
   draft only knows what column names say, and people say "revenue", not "total amount".
   Drop facts nobody asks about, give money measures their usual names, and add phrasing
   (see `databases/adventureworks/phrasing.yaml`, where every key is commented). Many-to-many
   links (a film in several categories) can't be followed by a plan; add a node for the view
   you want if you need one.
   In `catalog.yaml` under `project:` you can set `train_db` (where training plans run; use a
   copy whose dates reach today, so "last month" has data) and `test_db`.
3. **Check and generate training data:**
   ```
   scripts/prepare.sh databases/mydb
   ```
   This runs `coverage.py` (every column is used or excluded), `compile.py --selftest` (every
   entry compiles and runs), writes `data/catalog.json` and `data/plan.gbnf`, the tests (if
   `tests.py` exists), about 16,000 question/plan pairs, and the MLX chat files.
4. **Build aq and llama.cpp** (llama.cpp is pinned to tag b11429 and linked statically):
   ```
   scripts/build-aq.sh databases/mydb
   ```
5. **Train** (LoRA, 6000 iterations by default; set ITERS to change it):
   ```
   MLX_PY=~/venvs/mlx031/bin/python CONVERT_PY=~/venvs/convert/bin/python scripts/train.sh databases/mydb
   ```
   The model lands in `~/Library/Application Support/aq/<model_file>`.
6. **Ask:** `aq/.build/release/aq "your question"`. With `tests.py`, `scripts/evaluate.sh
   databases/mydb` checks that aq and the Python engine agree and scores the model.

The catalog is embedded in the binary, so one aq build serves one database; build again for
another (or put a `catalog.json` in aq's data dir to override it). `aq --help` lists options:
`-n` prints the SQL only, `-p` shows the plan, `--json` and `--csv` change the output.

## How a question is answered

1. Normalize the words (lower case, punctuation out, plurals folded).
2. Look values up: one read-only `UNION ALL` query asks which of the question's phrases are
   values of a lookup dimension. Nothing is cached.
3. Add the hint line `Values: dim=value; …` (at most 6) under the question.
4. The model writes a plan (greedy decoding).
5. A `not_tracked` answer must be one of the catalog's reasons (exit code 3).
6. Compile the plan and `EXPLAIN` it; if that fails, generate again under a grammar that
   only admits catalog names.
7. Run it read-only with a 15 s timeout and print a table, CSV or JSON.

## AdventureWorks

The example expects the AdventureWorks sample in a database `Adventureworks`, and a copy
`adventureworks_shifted` with dates moved to end today (used for training and scoring).
