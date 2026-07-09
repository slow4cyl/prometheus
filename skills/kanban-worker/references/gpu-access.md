## GPU Access

A local RTX 5090 (32GB VRAM) is available for experiments. Your task body
includes GPU instructions when local inference is beneficial.

**Quick start:**
```bash
gpu_run status                                    # Check what's running
gpu_run inference --model qwen2.5-0.5b --prompt "test"  # Quick test
gpu_run inference --model qwen2.5-7b --input data.jsonl --output results.jsonl  # Batch
gpu_run embedding --model nomic-embed --input docs.jsonl --output embeddings.npy

# GPU ML operations (auto-detected for PCA/encoding/LR experiments)
gpu_run gpu_ml encode --texts "..." --output emb.npy          # 42x faster
gpu_run gpu_ml pca --input X.npy --output X_pca.npy --components 64  # 9x faster
gpu_run gpu_ml lr --X features.npy --y labels.npy --output results   # 9x faster
gpu_run gpu_ml encode --texts "..." --queue --wait  # Serialized via queue
```

**Drop-in sklearn (change ONE import, same API, 5-8x faster):**
```python
# BEFORE: from sklearn.linear_model import LogisticRegression
# AFTER:  from gpu_sklearn.linear_model import LogisticRegression
# Works for StandardScaler, PCA, KNN too. Falls back to sklearn if GPU unavailable.
# NOTE: GPU is only faster for datasets >5K samples with >50 features.
# For small injection detection datasets (16K samples, 19 features), CPU is faster.
```

**Key points:**
- Models auto-download on first use (no manual setup)
- Multiple workers share the same server (model-instance lifecycle)
- Check `gpu_run status` before requesting — don't hog if others need it
- Training gets exclusive GPU lock — avoid unless your task specifically requires it
- Load the `gpu-toolset` skill for full documentation

**When to use GPU vs API:**
- GPU: batch processing (saves API costs), local model testing, embedding generation
- API: quick single queries, when GPU is busy, when you need mimo-v2.5 quality
