#!/usr/bin/env python3
"""
gpu_route — Auto-detect GPU-capable experiments and generate GPU instructions.

Scans experiment descriptions for operations that benefit from GPU offloading:
  - Sentence encoding / embeddings → gpu_ml.py encode (42x faster)
  - PCA / dimensionality reduction → gpu_ml.py pca (9x faster)
  - LogisticRegression / linear classifiers → gpu_ml.py lr (9x faster)

Usage:
  python3 gpu_route.py detect "experiment description text"
  python3 gpu_route.py detect --file experiment.py
  python3 gpu_route.py inject --title "exp_1234: PCA on hidden states" --body "..."
  python3 gpu_route.py batch-inject --tasks tasks.json --output routed_tasks.json
"""

import json
import re
import sys
import argparse
from typing import List, Dict, Optional

# ── Detection patterns ──

"""CLI tool: Gpu Route.

Usage: python3 gpu_route.py [options]
"""


GPU_OPS = {
    "encode": {
        "patterns": [
            r"sentence.?transform",
            r"embedding.{0,20}(encode|generat|comput)",
            r"(encode|encoding).{0,20}(text|sentences|passages)",
            r"nomic.?embed",
            r"hidden.?state.{0,20}(extract|extractor)",
            r"model\.encode\(",
            r"cosine.?similarity",
            r"vector.{0,10}(represent|creat)",
            r"embedding.{0,10}(f1|auc|score|metric)",
            r"neural.{0,10}embed",
            r"embed.{0,10}(transfer|distill)",
        ],
        "speedup": "42x",
        "gpu_cmd": "python3 ~/.hermes/scripts/gpu_ml.py encode",
        "description": "Sentence encoding on GPU (42x faster than CPU)",
        "min_samples": 100,  # Below this, CPU is fine
    },
    "pca": {
        "patterns": [
            r"\bpca\b",
            r"dimensionality.?reduc",
            r"svd.{0,20}(decompos|reduc)",
            r"feature.?select.{0,20}(\d+)",
            r"reduce.{0,10}(to|down|dimension)",
            r"n_components",
        ],
        "speedup": "9x",
        "gpu_cmd": "python3 ~/.hermes/scripts/gpu_ml.py pca",
        "description": "PCA via GPU SVD (9x faster than sklearn)",
        "min_samples": 500,
    },
    "lr": {
        "patterns": [
            r"logistic.?regress",
            r"\blr\b.{0,10}(fit|train|predict|classif)",
            r"linear.?classif",
            r"sklearn\.linear_model",
            r"cross_val_predict",
            r"lbfgs|newton.?cg|saga",
            r"\bSVM\b.{0,10}(linear|classif)",
            r"tf.?idf",
            r"classif.{0,20}(inject|attack|adversar)",
            r"detect.{0,20}(inject|attack|adversar)",
            r"injection.{0,10}(detect|classif|distribut)",
        ],
        "speedup": "9x",
        "gpu_cmd": "python3 ~/.hermes/scripts/gpu_ml.py lr",
        "description": "LogisticRegression on GPU (9x faster, ~3% accuracy gap)",
        "min_samples": 1000,
    },
}


def detect_ops(text: str) -> List[Dict]:
    """Detect GPU-capable operations in experiment text."""
    found = []
    text_lower = text.lower()

    for op_name, op_info in GPU_OPS.items():
        for pattern in op_info["patterns"]:
            if re.search(pattern, text_lower):
                found.append({
                    "operation": op_name,
                    "speedup": op_info["speedup"],
                    "description": op_info["description"],
                    "gpu_cmd": op_info["gpu_cmd"],
                    "pattern_matched": pattern,
                })
                break  # One match per operation is enough

    return found


def estimate_dataset_size(text: str) -> str:
    """Estimate dataset size from experiment description.

    Returns: 'small' (<5K), 'medium' (5K-50K), 'large' (>50K), 'unknown'
    """
    text_lower = text.lower()

    # Explicit size mentions
    large_patterns = [r"50k", r"100k", r"200k", r"500k", r"1m\b", r"million",
                      r"large.?scale", r"big.?data", r"full.?dataset"]
    medium_patterns = [r"10k", r"20k", r"30k", r"40k", r"\b50k\b",
                       r"medium.?scale", r"real.?world.?data"]
    small_patterns = [r"1k\b", r"2k\b", r"3k\b", r"500\b", r"100\b",
                      r"small.?scale", r"toy", r"synthetic"]

    for p in large_patterns:
        if re.search(p, text_lower):
            return "large"
    for p in medium_patterns:
        if re.search(p, text_lower):
            return "medium"
    for p in small_patterns:
        if re.search(p, text_lower):
            return "small"

    # Feature count hints
    high_feat = [r"\d{3,}\s*feature", r"high.?dimension", r"200.?feature",
                 r"500.?feature", r"1000.?feature"]
    for p in high_feat:
        if re.search(p, text_lower):
            return "medium"  # high features = medium effective size

    return "unknown"


def generate_gpu_instructions(ops: List[Dict], dataset_size: str = "unknown") -> str:
    """Generate GPU-specific instructions for detected operations.

    dataset_size: 'small' (<5K samples), 'medium' (5K-50K), 'large' (>50K), 'unknown'
    For small datasets, only inject encoding/PCA (always beneficial).
    For medium/large, also inject gpu_sklearn for LR/KNN.
    """
    if not ops:
        return ""

    # Separate always-GPU ops from size-dependent ops
    always_gpu = ["encode", "pca"]  # Always worth GPU regardless of dataset size
    size_dependent = ["lr", "knn", "feature_selection"]  # Only for medium/large

    lines = ["GPU ML OPS — your experiment uses GPU-accelerated operations:"]
    lines.append(f"  GPU ML script: python3 ~/.hermes/scripts/gpu_ml.py")
    lines.append("")

    for op in ops:
        is_size_dependent = op["operation"] in size_dependent
        is_small = dataset_size in ("small", "unknown") and is_size_dependent

        if is_small:
            # Skip gpu_sklearn for small datasets — CPU is faster
            lines.append(f"  • {op['operation'].upper()}: {op['description']}")
            lines.append(f"    ⚠ Dataset appears small — CPU sklearn is faster for this.")
            lines.append(f"    GPU overhead exceeds benefit for <20K samples with <50 features.")
            lines.append("")
            continue

        lines.append(f"  • {op['operation'].upper()}: {op['description']}")
        if op["operation"] == "encode":
            lines.append(f"    CPU: SentenceTransformer.encode() — ~159 texts/sec")
            lines.append(f"    GPU: gpu_ml.py encode --texts \"...\" --output emb.npy — ~6,700/sec")
            lines.append(f"    For batch encoding: gpu_ml.py encode --input texts.jsonl --output emb.npy")
        elif op["operation"] == "pca":
            lines.append(f"    CPU: sklearn.decomposition.PCA — ~0.8s for 2000×512")
            lines.append(f"    GPU: gpu_ml.py pca --input X.npy --output X_pca.npy --components N")
        elif op["operation"] == "lr":
            lines.append(f"    CPU: sklearn.linear_model.LogisticRegression — ~1.8s for 5000×200")
            lines.append(f"    GPU: gpu_ml.py lr --X features.npy --y labels.npy --output results")
            lines.append(f"    Note: GPU accuracy ~3% lower than sklearn. Use for screening, not final eval.")
        lines.append("")

    # Only inject gpu_sklearn import instructions for medium/large datasets
    if dataset_size in ("medium", "large"):
        has_lr_or_knn = any(op["operation"] in size_dependent for op in ops)
        if has_lr_or_knn:
            lines.append("  GPU SKLEARN DROP-IN (for medium/large datasets >5K samples):")
            lines.append("    from gpu_sklearn.linear_model import LogisticRegression  # 5-8x faster")
            lines.append("    from gpu_sklearn.neighbors import KNeighborsClassifier  # 2.5x faster")
            lines.append("    from gpu_sklearn.preprocessing import StandardScaler  # 5x faster")
            lines.append("    from gpu_sklearn.decomposition import PCA  # 9x faster")
            lines.append("    Same API as sklearn. Identical accuracy. Falls back to CPU if GPU unavailable.")
            lines.append("")

    return "\n".join(lines)


def inject_gpu_into_task(title: str, body: str) -> tuple:
    """Analyze a task and inject GPU instructions if applicable."""
    # Combine title and body for detection
    combined = f"{title} {body}"
    ops = detect_ops(combined)
    dataset_size = estimate_dataset_size(combined)

    if not ops:
        return body, False

    # Generate GPU instructions with dataset size context
    gpu_instructions = generate_gpu_instructions(ops, dataset_size)

    # Insert after GPU AVAILABLE section, before CRITICAL RULES
    marker = "CRITICAL RULES:"
    if marker in body:
        parts = body.split(marker, 1)
        new_body = parts[0] + gpu_instructions + "\n" + marker + parts[1]
    else:
        # Append before the last section
        new_body = body + "\n" + gpu_instructions

    return new_body, True


def batch_inject(tasks: list) -> list:
    """Inject GPU instructions into a batch of tasks."""
    routed = 0
    for task in tasks:
        title = task.get("title", "")
        body = task.get("body", "")
        new_body, was_injected = inject_gpu_into_task(title, body)
        if was_injected:
            task["body"] = new_body
            task["gpu_routed"] = True
            routed += 1
        else:
            task["gpu_routed"] = False

    print(f"GPU routed: {routed}/{len(tasks)} tasks", file=sys.stderr)
    return tasks


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="GPU routing for experiments")
    sub = parser.add_subparsers(dest="command")

    # detect
    p_det = sub.add_parser("detect", help="Detect GPU-capable operations in text")
    p_det.add_argument("text", nargs="?", help="Text to analyze")
    p_det.add_argument("--file", help="Read text from file")

    # inject
    p_inj = sub.add_parser("inject", help="Inject GPU instructions into a task")
    p_inj.add_argument("--title", required=True)
    p_inj.add_argument("--body", required=True)

    # batch-inject
    p_batch = sub.add_parser("batch-inject", help="Inject GPU instructions into task batch")
    p_batch.add_argument("--tasks", required=True, help="JSON file with task list")
    p_batch.add_argument("--output", help="Output file (default: stdout)")

    args = parser.parse_args()

    if args.command == "detect":
        text = args.text or ""
        if args.file:
            with open(args.file) as f:
                text = f.read()
        ops = detect_ops(text)
        if ops:
            print(json.dumps(ops, indent=2))
            print(generate_gpu_instructions(ops))
        else:
            print("No GPU-accelerable operations detected.")

    elif args.command == "inject":
        new_body, injected = inject_gpu_into_task(args.title, args.body)
        if injected:
            print("GPU instructions injected:")
            print(new_body)
        else:
            print("No GPU operations detected. Body unchanged.")

    elif args.command == "batch-inject":
        with open(args.tasks) as f:
            tasks = json.load(f)
        routed = batch_inject(tasks)
        output = json.dumps(routed, indent=2)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Written to {args.output}")
        else:
            print(output)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
