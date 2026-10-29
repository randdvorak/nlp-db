#!/bin/bash
# LoRA-train the plan model on Apple silicon (MLX), fuse it, convert to a Q8_0 gguf and
# install it where dbq looks for it:
#   scripts/train.sh databases/<name>
# Run scripts/prepare.sh first. Settings (environment):
#   MLX_PY      python with mlx 0.31 + mlx-lm  (default: python3; mlx 0.32 went NaN for us)
#   CONVERT_PY  python with llama.cpp's convert requirements (default: python3)
#   QUANTIZE    llama-quantize (default: llama-quantize from Homebrew)
#   ITERS       training iterations (default 6000, about 1.6 epochs of 16,000 pairs)
#   DBQ_HOME     dbq's data dir (default ~/Library/Application Support/dbq)
source "$(dirname "$0")/common.sh"
MLX_PY="${MLX_PY:-python3}"; CONVERT_PY="${CONVERT_PY:-python3}"; QUANTIZE="${QUANTIZE:-llama-quantize}"
BASE="${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}"
WORK="$DBQ_PROJECT/work"; mkdir -p "$WORK"
MODEL_FILE="$(setting 'SETTINGS.get("model_file", "model.gguf")')"
DEST="${DBQ_HOME:-$HOME/Library/Application Support/dbq}"
CONVERT="$ROOT/dbq/vendor/llama.cpp-src/convert_hf_to_gguf.py"
[ -f "$CONVERT" ] || { echo "no $CONVERT: run dbq/scripts/build-llama.sh first" >&2; exit 2; }

"$MLX_PY" "$ROOT/training/train_guarded.py" --config "$ROOT/training/lora.yaml" \
    --model "$BASE" --data "$DBQ_PROJECT/data/plans" --adapter-path "$WORK/adapters" \
    --iters "${ITERS:-6000}" 2>&1 | tee "$WORK/train.log"

SNAP=$("$MLX_PY" -c "from huggingface_hub import snapshot_download as s; print(s('$BASE'))")/
rm -rf "$WORK/fused"
"$MLX_PY" -m mlx_lm fuse --model "$SNAP" --adapter-path "$WORK/adapters" --save-path "$WORK/fused"
# llama.cpp's converter wants the base model's own tokenizer files, not MLX's rewrite.
mv "$WORK/fused/tokenizer_config.json" "$WORK/fused/tokenizer_config.mlx.json"
for f in tokenizer.json tokenizer_config.json vocab.json merges.txt generation_config.json; do cp -L "$SNAP$f" "$WORK/fused/"; done
"$CONVERT_PY" "$CONVERT" "$WORK/fused" --outtype f16 --outfile "$WORK/model-f16.gguf"
"$QUANTIZE" "$WORK/model-f16.gguf" "$WORK/model-q8_0.gguf" Q8_0
mkdir -p "$DEST"
cp "$WORK/model-q8_0.gguf" "$DEST/$MODEL_FILE"
echo "installed $DEST/$MODEL_FILE"
