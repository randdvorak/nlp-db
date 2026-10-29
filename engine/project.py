"""The database folder the engine works on: `--project DIR` on the command line (taken out of
sys.argv) or $DBQ_PROJECT. It holds

  catalog.yaml     the semantic catalog: tables, joins, facts, measures, dimensions, flags,
                   time words, plus `project:` settings (name, system prompt, connections)
  phrasing.yaml    optional: how people ask about this database (question wording, made-up
                   value shapes, weights); generic defaults otherwise
  tests.py         optional: hand-written test questions -> data/tests.jsonl
  data/            generated files
"""
import os
import sys

import yaml


def _find():
    if "--project" in sys.argv:
        i = sys.argv.index("--project")
        path = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return path
    if os.environ.get("DBQ_PROJECT"):
        return os.environ["DBQ_PROJECT"]
    sys.exit("no project: pass --project databases/<name> or set DBQ_PROJECT")


DIR = os.path.abspath(_find())
DATA = os.path.join(DIR, "data")
os.makedirs(DATA, exist_ok=True)


def _load(name):
    path = os.path.join(DIR, name)
    return yaml.safe_load(open(path, encoding="utf-8")) or {} if os.path.exists(path) else {}


CATALOG = _load("catalog.yaml")
if not CATALOG:
    sys.exit(f"no catalog.yaml in {DIR}")
PHRASING = _load("phrasing.yaml")
SETTINGS = CATALOG.get("project", {})
NAME = SETTINGS.get("name", os.path.basename(DIR))
# The system prompt is part of what the model was trained on: dbq reads the same text from catalog.json.
SYSTEM_PROMPT = SETTINGS.get("system_prompt") or (
    f"You translate questions about the {NAME} database into a JSON query plan that uses "
    "only the catalog's measure, dimension, flag and period names. Copy filter values exactly as "
    "listed after 'Values:'. Reply with the plan only.")
# Where dbq and the scripts read values and run tests, and where training plans are run.
CONNINFO = SETTINGS.get("conninfo", f"dbname={os.path.basename(DIR)}")
TRAIN_DB = SETTINGS.get("train_db", CONNINFO)
TEST_DB = SETTINGS.get("test_db", TRAIN_DB)


def path(*parts):
    return os.path.join(DATA, *parts)
