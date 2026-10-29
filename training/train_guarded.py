"""mlx_lm.lora with gradient-norm clipping and a guard against non-finite gradients.

Plain mlx_lm.lora (mlx-lm 0.32.2 / mlx 0.32.4) went NaN after 20-210 iterations on
Qwen2.5-Coder-7B-Instruct-4bit at several learning rates and LoRA scales. This wraps
Optimizer.update so every step clips the global grad norm and zeroes the gradient
when it contains inf/nan, then runs the normal CLI.

Usage: python train_guarded.py <same args as mlx_lm.lora>
"""
import os
import mlx.core as mx
import mlx.optimizers as optim
from mlx.utils import tree_flatten, tree_map

MAX_NORM = float(os.environ.get("MAX_GRAD_NORM", "1.0"))
_update = optim.Optimizer.update

def guarded_update(self, model, gradients):
    gradients, norm = optim.clip_grad_norm(gradients, MAX_NORM)
    ok = mx.isfinite(norm)
    for _, g in tree_flatten(gradients):
        ok = mx.logical_and(ok, mx.all(mx.isfinite(g)))
    gradients = tree_map(lambda g: mx.where(ok, g, mx.zeros_like(g)), gradients)
    return _update(self, model, gradients)

optim.Optimizer.update = guarded_update

if __name__ == "__main__":
    from mlx_lm.lora import main
    main()
