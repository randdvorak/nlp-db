"""data/pairs.jsonl + data/tests.jsonl -> chat files for MLX LoRA (plan output).

  python3 make_plan_dataset.py --project databases/<name>

  data/plans/train.jsonl, valid.jsonl   (95/5 split of the generated pairs, seed 7)
  data/plans/tests.jsonl                (the hand-written questions, if any, with plan targets;
                                         their hints are looked up in the project's database)
"""
import json, os, random
import project as P
import compile as C
import values as V


def plan_text(plan):
    return json.dumps(plan, separators=(",", ":"), ensure_ascii=False)


def chat(prompt, plan):
    return {"messages": [{"role": "system", "content": P.SYSTEM_PROMPT}, {"role": "user", "content": prompt},
                         {"role": "assistant", "content": plan_text(plan)}]}


def read(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")] if os.path.exists(path) else []


out = P.path("plans")
os.makedirs(out, exist_ok=True)
pairs = read(P.path("pairs.jsonl"))
hand = read(P.path("tests.jsonl"))
# Never train on a test question.
hand_q = {r["question"].lower().rstrip("?.") for r in hand}
pairs = [p for p in pairs if p["question"].lower().rstrip("?.") not in hand_q]
random.Random(7).shuffle(pairs)
n_valid = len(pairs) // 20
for name, rows in (("valid", pairs[:n_valid]), ("train", pairs[n_valid:])):
    with open(os.path.join(out, f"{name}.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(chat(r["prompt"], r["plan"]), ensure_ascii=False) + "\n")
if hand:
    matcher = V.Matcher(C.connect())
    with open(os.path.join(out, "tests.jsonl"), "w", encoding="utf-8") as f:
        for r in hand:
            row = chat(V.prompt(r["question"], matcher), r["plan"])
            row["sql"] = None if "not_tracked" in r["plan"] else C.compile_plan(r["plan"])
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
print(f"train {len(pairs) - n_valid}, valid {n_valid}, tests {len(hand)} -> {out}")
