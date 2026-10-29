"""Generate a plan for each row of a chat file with an MLX model + LoRA adapter.

  python predict_plans.py --model Qwen/Qwen2.5-Coder-1.5B-Instruct --adapter-path work/adapters \
      --data databases/<name>/data/plans/tests.jsonl --out work/predictions.jsonl
Writes one {"text": raw output} per line, in input order.
"""
import argparse, json, time
from mlx_lm import load, generate

ap = argparse.ArgumentParser()
ap.add_argument("--model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
ap.add_argument("--adapter-path")
ap.add_argument("--data", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--max-tokens", type=int, default=250)
args = ap.parse_args()

model, tok = load(args.model, adapter_path=args.adapter_path)
tok.add_eos_token("<|im_end|>")
rows = [json.loads(l) for l in open(args.data, encoding="utf-8")]
t0 = time.time()
with open(args.out, "w", encoding="utf-8") as f:
    for i, r in enumerate(rows, 1):
        msgs = [m for m in r["messages"] if m["role"] in ("system", "user")]
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        out = generate(model, tok, prompt=prompt, max_tokens=args.max_tokens)
        f.write(json.dumps({"text": out.split("<|im_end|>")[0].strip()}, ensure_ascii=False) + "\n")
        f.flush()
        if i % 25 == 0 or i == len(rows):
            print(f"{i}/{len(rows)}  {time.time() - t0:.0f}s", flush=True)
print(f"{(time.time() - t0) / len(rows):.2f}s per question -> {args.out}")
