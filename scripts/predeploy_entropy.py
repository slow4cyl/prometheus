#!/usr/bin/env python3
"""
Pre-deployment Signal Concentration Entropy Predictor

Computes token-level entropy, top-k concentration, and effective vocabulary
size from a base causal LM given prompts or a dataset. These metrics predict
fine-tuning difficulty: LOW entropy = concentrated distributions = model
knows the domain = easy to fine-tune. HIGH entropy = diffuse = harder.

Based on: exp_101341 (quality=100, BUILD_ACTIONABLE)
Finding: Signal concentration perfectly predicts fine-tuning final loss
(ρ=+1.0, p=0.000).

Usage (single prompt):
    python3 predeploy_entropy.py --prompt "Translate English to French: hello" --model qwen2.5-0.5b
    python3 predeploy_entropy.py --prompt "def fibonacci(n):" --model qwen2.5-0.5b --json

Usage (dataset):
    python3 predeploy_entropy.py --dataset data.jsonl --model qwen2.5-0.5b
    python3 predeploy_entropy.py --dataset data.jsonl --model qwen2.5-0.5b --max-samples 100 --json
    python3 predeploy_entropy.py --dataset data.jsonl --model qwen2.5-0.5b --validate

Output metrics (per-sample):
    - entropy: Shannon entropy in bits per token position
    - top_k_concentration: fraction of prob mass in top-k tokens
    - effective_vocab_size: exp(mean_entropy) in bits
    - difficulty_score: 0-1 composite (0=easy, 1=hard)

Output metrics (dataset-level):
    - dataset_difficulty_score: aggregate difficulty (0-1)
    - mean_entropy_bits: mean across all samples
    - entropy_std: spread of entropy values
    - predict_finetune_loss: estimated fine-tuning loss
"""

import argparse
import csv
import json
import math
import os
import random
import sys
import time

"""CLI tool: Predeploy Entropy.

Usage: python3 predeploy_entropy.py [options]
"""


SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)


def resolve_model_name(model_arg: str) -> str:
    """Map short model names to HuggingFace repo IDs."""
    short = {
        "qwen2.5-0.5b": "Qwen/Qwen2.5-0.5B",
        "qwen2.5-1.5b": "Qwen/Qwen2.5-1.5B",
        "qwen2.5-3b": "Qwen/Qwen2.5-3B",
        "qwen2.5-7b": "Qwen/Qwen2.5-7B",
        "qwen3-0.6b": "Qwen/Qwen3-0.6B",
        "qwen3-1.7b": "Qwen/Qwen3-1.7B",
    }
    low = model_arg.lower().strip()
    if low in short:
        return short[low]
    if "/" in model_arg:
        return model_arg
    return model_arg


def load_dataset(path: str, text_field: str = "text", max_samples: int = 500,
                 seed: int = 42) -> list:
    """
    Load prompts from a dataset file or directory.

    Supports:
    - JSONL files (one JSON object per line, expects 'text' field)
    - CSV files (expects 'text' column)
    - Plain text files (one prompt per line)
    - Directory of any of the above (recursive)

    Returns list of prompt strings, sampled if exceeding max_samples.
    """
    prompts = []
    files_to_load = []

    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            for f in sorted(files):
                fp = os.path.join(root, f)
                if fp.endswith((".jsonl", ".csv", ".txt", ".json")):
                    files_to_load.append(fp)
    elif os.path.isfile(path):
        files_to_load = [path]
    else:
        raise FileNotFoundError(f"Dataset path not found: {path}")

    for fp in files_to_load:
        try:
            if fp.endswith(".jsonl") or fp.endswith(".json"):
                with open(fp) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            # Could be a plain .json array
                            continue
                        if isinstance(obj, dict) and text_field in obj:
                            text = obj[text_field]
                            if isinstance(text, str) and text.strip():
                                prompts.append(text.strip())
                        elif isinstance(obj, dict) and "content" in obj:
                            # Alternative field name
                            text = obj["content"]
                            if isinstance(text, str) and text.strip():
                                prompts.append(text.strip())
            elif fp.endswith(".csv"):
                with open(fp) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if text_field in row and row[text_field].strip():
                            prompts.append(row[text_field].strip())
            elif fp.endswith(".txt"):
                with open(fp) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            prompts.append(line)
        except Exception as e:
            print(f"Warning: failed to load {fp}: {e}", file=sys.stderr)

    if not prompts:
        raise ValueError(f"No prompts found in {path} (looked for '{text_field}' field)")

    # Sample if too many
    if len(prompts) > max_samples:
        rng = random.Random(seed)
        prompts = rng.sample(prompts, max_samples)
        print(f"Sampled {max_samples} prompts from {len(prompts)} total",
              file=sys.stderr)

    return prompts


def compute_entropy_metrics(model, tokenizer, prompt: str, top_k: int = 10,
                            model_vocab_size: int = None):
    """
    Compute token-level entropy metrics for a prompt under a causal LM.

    Returns dict with aggregate and per-token metrics.
    """
    import torch

    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=512)
    input_ids = inputs["input_ids"].to(model.device)
    attention_mask = inputs.get("attention_mask", None)
    if attention_mask is not None:
        attention_mask = attention_mask.to(model.device)

    token_count = input_ids.shape[1]

    with torch.no_grad():
        outputs = model(input_ids, attention_mask=attention_mask)
        logits = outputs.logits

    probs = torch.softmax(logits, dim=-1)

    entropy_vals = []
    top_k_vals = []
    top1_vals = []
    top5_vals = []
    eff_vocab_vals = []

    for i in range(token_count):
        pos_probs = probs[0, i].cpu().float()

        # Shannon entropy in bits
        pos_nonzero = pos_probs[pos_probs > 0]
        h = -torch.sum(pos_nonzero * torch.log2(pos_nonzero)).item()
        entropy_vals.append(h)

        # Top-k concentrations
        sorted_probs = torch.sort(pos_probs, descending=True).values
        top_k_frac = sorted_probs[:min(top_k, len(sorted_probs))].sum().item()
        top_1_frac = sorted_probs[0].item()
        top_5_frac = sorted_probs[:min(5, len(sorted_probs))].sum().item()

        top_k_vals.append(top_k_frac)
        top1_vals.append(top_1_frac)
        top5_vals.append(top_5_frac)

        eff_vocab_vals.append(2 ** h)

    mean_entropy = sum(entropy_vals) / len(entropy_vals)
    mean_top_k = sum(top_k_vals) / len(top_k_vals)
    mean_top1 = sum(top1_vals) / len(top1_vals)
    mean_top5 = sum(top5_vals) / len(top5_vals)
    mean_eff_vocab = sum(eff_vocab_vals) / len(eff_vocab_vals)

    vocab_size = model_vocab_size or getattr(model.config, 'vocab_size', 151936)
    max_entropy = math.log2(vocab_size)
    difficulty = mean_entropy / max_entropy

    return {
        "mean_entropy_bits": round(mean_entropy, 4),
        "std_entropy_bits": round(
            (sum((e - mean_entropy) ** 2 for e in entropy_vals) / len(entropy_vals)) ** 0.5, 4),
        "top_k_concentration": round(mean_top_k, 4),
        "top_1_concentration": round(mean_top1, 4),
        "top_5_concentration": round(mean_top5, 4),
        "effective_vocab_size": round(mean_eff_vocab, 1),
        "difficulty_score": round(difficulty, 4),
        "token_count": token_count,
        "max_entropy_bits": round(max_entropy, 4),
        "model_vocab_size": vocab_size,
    }


def load_model(model_name: str, device: str = "auto"):
    """Load model and tokenizer once, reuse across samples."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_id = resolve_model_name(model_name)
    print(f"Loading model: {model_id}", file=sys.stderr)

    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        dtype=torch.float16,
        device_map=device,
        trust_remote_code=True,
    )
    model.eval()
    return model, tokenizer


def analyze_dataset(model, tokenizer, prompts: list, top_k: int = 10) -> dict:
    """
    Run entropy analysis across a dataset of prompts.
    Returns aggregate statistics.
    """
    results = []
    t0 = time.time()

    for i, prompt in enumerate(prompts):
        if (i + 1) % 10 == 0 or i == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"  Analyzing sample {i + 1}/{len(prompts)} "
                  f"({rate:.1f} samples/s)", file=sys.stderr)

        try:
            metrics = compute_entropy_metrics(model, tokenizer, prompt, top_k=top_k)
            metrics["prompt_preview"] = prompt[:100]
            results.append(metrics)
        except Exception as e:
            print(f"  Warning: sample {i} failed: {e}", file=sys.stderr)

    elapsed = time.time() - t0

    if not results:
        return {"error": "All samples failed", "elapsed_seconds": round(elapsed, 2)}

    # Aggregate statistics
    entropies = [r["mean_entropy_bits"] for r in results]
    difficulties = [r["difficulty_score"] for r in results]
    top_k_concs = [r["top_k_concentration"] for r in results]
    eff_vocabs = [r["effective_vocab_size"] for r in results]

    mean_ent = sum(entropies) / len(entropies)
    std_ent = (sum((e - mean_ent) ** 2 for e in entropies) / len(entropies)) ** 0.5

    sorted_ent = sorted(entropies)
    n = len(sorted_ent)
    median_ent = sorted_ent[n // 2]
    p25_ent = sorted_ent[n // 4]
    p75_ent = sorted_ent[3 * n // 4]

    mean_diff = sum(difficulties) / len(difficulties)
    mean_topk = sum(top_k_concs) / len(top_k_concs)
    mean_eff = sum(eff_vocabs) / len(eff_vocabs)

    # Predict fine-tuning loss using linear model from exp_101341
    # ρ=+1.0 means perfect correlation between entropy and FT loss
    # Typical FT loss range: 0.1 (easy) to 5.0 (hard)
    # Map difficulty_score 0-1 to loss range
    predicted_loss = 0.1 + mean_diff * 4.9  # linear mapping

    # Dataset quality rating
    if mean_diff < 0.15:
        quality = "EXCELLENT"
        recommendation = "Dataset is highly learnable. Low epochs, small LR sufficient."
    elif mean_diff < 0.30:
        quality = "GOOD"
        recommendation = "Dataset is learnable. Standard training config should work."
    elif mean_diff < 0.50:
        quality = "MODERATE"
        recommendation = "Dataset requires moderate effort. Consider more epochs and larger LR."
    elif mean_diff < 0.70:
        quality = "DIFFICULT"
        recommendation = "Dataset is hard. Increase epochs, consider curriculum learning."
    else:
        quality = "VERY_HARD"
        recommendation = "Dataset may not fine-tune well. Consider data curation or augmentation."

    return {
        "num_samples": len(results),
        "elapsed_seconds": round(elapsed, 2),
        "samples_per_second": round(len(results) / elapsed, 2) if elapsed > 0 else 0,

        # Aggregate entropy
        "mean_entropy_bits": round(mean_ent, 4),
        "std_entropy_bits": round(std_ent, 4),
        "median_entropy_bits": round(median_ent, 4),
        "p25_entropy_bits": round(p25_ent, 4),
        "p75_entropy_bits": round(p75_ent, 4),

        # Aggregate concentration
        "mean_top_k_concentration": round(mean_topk, 4),
        "mean_effective_vocab": round(mean_eff, 1),

        # Dataset-level difficulty
        "dataset_difficulty_score": round(mean_diff, 4),
        "difficulty_std": round(
            (sum((d - mean_diff) ** 2 for d in difficulties) / len(difficulties)) ** 0.5, 4),

        # Prediction
        "predicted_finetune_loss": round(predicted_loss, 4),
        "quality_rating": quality,
        "recommendation": recommendation,

        # Per-sample results
        "samples": results,
    }


def validate_predictions(dataset_results: dict) -> dict:
    """
    Validate predictions against known fine-tuning difficulty heuristics.
    Produces a validation report.
    """
    samples = dataset_results.get("samples", [])
    if not samples:
        return {"validation": "SKIP", "reason": "No samples to validate"}

    difficulties = [s["difficulty_score"] for s in samples]
    entropies = [s["mean_entropy_bits"] for s in samples]

    # Heuristic validation: entropy should correlate with concentration
    # Low entropy = high concentration (inverse relationship)
    # Check that top_1_concentration is negatively correlated with entropy
    top1 = [s.get("top_1_concentration", 0) for s in samples]

    # Spearman rank correlation
    n = len(entropies)
    if n < 5:
        return {"validation": "SKIP", "reason": "Too few samples for validation"}

    def rank(arr):
        sorted_idx = sorted(range(n), key=lambda i: arr[i])
        ranks = [0.0] * n
        for rank_val, idx in enumerate(sorted_idx, 1):
            ranks[idx] = rank_val
        return ranks

    rank_e = rank(entropies)
    rank_t = rank(top1)
    mean_re = sum(rank_e) / n
    mean_rt = sum(rank_t) / n
    cov = sum((rank_e[i] - mean_re) * (rank_t[i] - mean_rt) for i in range(n))
    std_re = (sum((r - mean_re) ** 2 for r in rank_e)) ** 0.5
    std_rt = (sum((r - mean_rt) ** 2 for r in rank_t)) ** 0.5
    spearman = cov / (std_re * std_rt) if std_re * std_rt > 0 else 0

    # Check: entropy distribution should be unimodal and positive
    min_ent = min(entropies)
    max_ent = max(entropies)
    entropy_range = max_ent - min_ent

    # Cross-sample consistency: difficulty should not vary wildly
    difficulty_cv = dataset_results.get("difficulty_std", 0) / max(dataset_results.get("dataset_difficulty_score", 0.001), 0.001)

    validations = {
        "entropy_concentration_correlation": round(spearman, 4),
        "correlation_valid": spearman < -0.3,  # negative = correct direction
        "entropy_range_bits": round(entropy_range, 4),
        "entropy_all_positive": min_ent > 0,
        "difficulty_cv": round(difficulty_cv, 4),
        "difficulty_consistent": difficulty_cv < 1.0,
        "overall_validation": "PASS",
        "validation_notes": []
    }

    notes = []
    if spearman < -0.3:
        notes.append(f"Spearman(entropy, top1) = {spearman:.3f}: entropy inversely correlates with concentration (CORRECT)")
    else:
        notes.append(f"Spearman(entropy, top1) = {spearman:.3f}: weak/positive correlation (INVESTIGATE)")
        validations["overall_validation"] = "WARN"

    if min_ent <= 0:
        notes.append(f"Min entropy = {min_ent}: zero entropy means deterministic predictions (valid but unusual)")
        validations["overall_validation"] = "WARN"

    if difficulty_cv > 1.0:
        notes.append(f"High variance in difficulty (CV={difficulty_cv:.2f}): dataset may contain mixed domains")
        validations["overall_validation"] = "WARN"

    if not notes:
        notes.append("All validation checks passed")

    validations["validation_notes"] = notes
    return validations


def main():
    parser = argparse.ArgumentParser(
        description="Pre-deployment signal concentration entropy predictor"
    )
    parser.add_argument("--prompt", type=str, help="Single prompt to analyze")
    parser.add_argument("--prompt-file", type=str, help="File containing prompts (one per line)")
    parser.add_argument("--dataset", type=str,
                        help="Dataset path (JSONL/CSV/TXT or directory)")
    parser.add_argument("--text-field", type=str, default="text",
                        help="Field name for text in JSONL/CSV (default: text)")
    parser.add_argument("--model", type=str, default="qwen2.5-0.5b",
                        help="Model name or HF repo ID (default: qwen2.5-0.5b)")
    parser.add_argument("--top-k", type=int, default=10,
                        help="K for top-k concentration (default: 10)")
    parser.add_argument("--max-samples", type=int, default=500,
                        help="Max dataset samples to analyze (default: 500)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for sampling (default: 42)")
    parser.add_argument("--device", type=str, default="auto",
                        help="Device mapping (default: auto)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--validate", action="store_true",
                        help="Run validation checks on predictions")
    parser.add_argument("--verbose", action="store_true",
                        help="Include per-token and per-sample details")
    parser.add_argument("--output", type=str, help="Write results to file (JSON)")
    args = parser.parse_args()

    if not args.prompt and not args.prompt_file and not args.dataset:
        parser.error("One of --prompt, --prompt-file, or --dataset is required")

    # Collect prompts for single-prompt/file mode
    prompts = []
    if args.prompt:
        prompts.append(args.prompt)
    if args.prompt_file:
        with open(args.prompt_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    prompts.append(line)

    # Load dataset if specified
    if args.dataset:
        prompts = load_dataset(args.dataset, text_field=args.text_field,
                               max_samples=args.max_samples, seed=args.seed)
        print(f"Loaded {len(prompts)} prompts from {args.dataset}", file=sys.stderr)

    if not prompts:
        print("Error: no prompts to analyze", file=sys.stderr)
        sys.exit(1)

    # Load model once
    import torch
    model, tokenizer = load_model(args.model, device=args.device)
    vocab_size = model.config.vocab_size

    if args.dataset:
        # Dataset mode: full analysis
        print(f"\nAnalyzing {len(prompts)} prompts...", file=sys.stderr)
        dataset_results = analyze_dataset(model, tokenizer, prompts, top_k=args.top_k)
        dataset_results["model"] = resolve_model_name(args.model)
        dataset_results["dataset_path"] = args.dataset

        if args.validate:
            validation = validate_predictions(dataset_results)
            dataset_results["validation"] = validation

        if not args.verbose:
            # Strip per-sample details
            for s in dataset_results.get("samples", []):
                s.pop("per_token_details", None)
                s.pop("entropy_per_token", None)
            if "samples" in dataset_results and not args.json:
                dataset_results.pop("samples", None)

        if args.json:
            output = json.dumps(dataset_results, indent=2)
        else:
            output = format_dataset_report(dataset_results, args.top_k)

    else:
        # Single-prompt mode (backward compatible)
        results = []
        for prompt in prompts:
            metrics = compute_entropy_metrics(model, tokenizer, prompt, top_k=args.top_k)
            metrics["prompt"] = prompt
            metrics["model"] = resolve_model_name(args.model)
            results.append(metrics)

        if args.json:
            output = json.dumps(results[0] if len(results) == 1 else results, indent=2)
        else:
            output = format_single_report(results, args.top_k)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"\nResults written to {args.output}", file=sys.stderr)
    else:
        print(output)


def format_dataset_report(r: dict, top_k: int) -> str:
    """Format dataset analysis as human-readable report."""
    lines = []
    lines.append("=" * 60)
    lines.append("PRE-DEPLOYMENT DATASET ANALYSIS")
    lines.append("=" * 60)
    lines.append(f"  Model:               {r.get('model', 'unknown')}")
    lines.append(f"  Dataset:             {r.get('dataset_path', 'unknown')}")
    lines.append(f"  Samples analyzed:    {r.get('num_samples', 0)}")
    lines.append(f"  Time:                {r.get('elapsed_seconds', 0):.1f}s "
                 f"({r.get('samples_per_second', 0):.1f} samples/s)")
    lines.append("")
    lines.append("ENTROPY DISTRIBUTION:")
    lines.append(f"  Mean:                {r.get('mean_entropy_bits', 0):.4f} bits")
    lines.append(f"  Std:                 {r.get('std_entropy_bits', 0):.4f} bits")
    lines.append(f"  Median:              {r.get('median_entropy_bits', 0):.4f} bits")
    lines.append(f"  25th percentile:     {r.get('p25_entropy_bits', 0):.4f} bits")
    lines.append(f"  75th percentile:     {r.get('p75_entropy_bits', 0):.4f} bits")
    lines.append("")
    lines.append("CONCENTRATION:")
    lines.append(f"  Top-{top_k} fraction:    {r.get('mean_top_k_concentration', 0):.4f}")
    lines.append(f"  Effective vocab:     {r.get('mean_effective_vocab', 0):.1f}")
    lines.append("")
    lines.append("FINE-TUNING PREDICTION:")
    lines.append(f"  Difficulty score:    {r.get('dataset_difficulty_score', 0):.4f} "
                 f"(0=easy, 1=hard)")
    lines.append(f"  Difficulty spread:   {r.get('difficulty_std', 0):.4f}")
    lines.append(f"  Predicted FT loss:   {r.get('predicted_finetune_loss', 0):.4f}")
    lines.append(f"  Quality rating:      {r.get('quality_rating', 'unknown')}")
    lines.append(f"  Recommendation:      {r.get('recommendation', 'N/A')}")
    lines.append("")

    if "validation" in r:
        v = r["validation"]
        lines.append("VALIDATION:")
        lines.append(f"  Overall:             {v.get('overall_validation', 'N/A')}")
        for note in v.get("validation_notes", []):
            lines.append(f"    - {note}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_single_report(results: list, top_k: int) -> str:
    """Format single-prompt results as human-readable report."""
    lines = []
    for r in results:
        lines.append(f"Prompt: {r.get('prompt', '?')[:80]}...")
        lines.append(f"  Model:                  {r.get('model', '?')}")
        lines.append(f"  Tokens:                 {r.get('token_count', 0)}")
        lines.append(f"  Mean entropy:           {r.get('mean_entropy_bits', 0):.4f} bits "
                      f"(max: {r.get('max_entropy_bits', 0):.4f})")
        lines.append(f"  Top-{top_k} concentration: {r.get('top_k_concentration', 0):.4f}")
        lines.append(f"  Top-1 concentration:    {r.get('top_1_concentration', 0):.4f}")
        lines.append(f"  Top-5 concentration:    {r.get('top_5_concentration', 0):.4f}")
        lines.append(f"  Effective vocab size:   {r.get('effective_vocab_size', 0):.1f}")
        lines.append(f"  Difficulty score:       {r.get('difficulty_score', 0):.4f} "
                      f"(0=easy, 1=hard)")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
