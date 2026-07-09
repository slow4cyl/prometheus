# Director Pass Manual Creation Worked Example (June 3 2026)

## Scenario

Board: 11 running tasks (9 experiments + 2 synthesis), 40 free workers, 50 queue items.
Batch creator returns 0 uncovered at default threshold. Manual fallback needed.

## Step-by-Step

### 1. Batch creator fails at all thresholds

```
$ python3 batch_create_tasks.py --count 15 --dry-run
Queue: 50 active items, 31 score >= 60
Workers: 43 free, 8 busy
Uncovered by running tasks: 0
Selected: 0 tasks

$ python3 batch_create_tasks.py --count 15 --dry-run --min-score 10
Queue: 50 active items, 50 score >= 10
Workers: 43 free, 7 busy
Uncovered by running tasks: 1
Selected: 1 tasks
```

Even at min-score 10, only 1 uncovered item. The batch creator checks against ALL 3,183 completed experiments, making everything appear covered.

### 2. Manual word-overlap scoring against RUNNING tasks only

Extract running experiment task titles, build word set, score each queue item:

```
Running experiment titles: 9
Running word set size: 87

LEAST covered (best candidates):
  [11] overlap=0.06 "Do subtle injections (short, normal-looking) defeat distillation..."
  [13] overlap=0.11 "Can raw ECE thresholding replace meta-classifiers..."
  [ 8] overlap=0.13 "Does transfer gap vary by tokenizer (BPE vs SentencePiece vs Unigram)?"
  [26] overlap=0.14 "What is the minimum non_latin_ratio threshold..."
  [ 6] overlap=0.14 "Does weighted voting (not majority) prevent weak-detector poisoning?"
  ...

Uncovered (overlap < 0.3): 22 items
```

### 3. Create tasks for top 10 uncovered items

```python
# Score queue items by word overlap with running titles
running_words = set()
for title in running_titles:
    for w in re.findall(r'\w+', title.lower()):
        running_words.add(w)

def overlap_score(item_text, rwords):
    item_words = set()
    for w in re.findall(r'\w+', item_text.lower()):
        item_words.add(w)
    if not item_words:
        return 0
    common = item_words.intersection(rwords)  # NOTE: .intersection(), NOT &
    return len(common) / min(len(item_words), len(rwords))
```

Created 10 tasks (exp_3268-exp_3277) assigned to free workers 1,4,6,8,14-19.

### 4. Also created synthesis task for 3 experiments outside existing synthesis range

exp_3260-3262 completed after the two running synthesis tasks (covering 3088-3259) were created. Assigned to a free worker, not prometheus-synthesis (which was already at per-profile cap).

### 5. Dispatch and verify

```
$ hermes kanban dispatch
Spawned: 11 (10 experiments + 1 synthesis)
```

## Key Difference from Batch Creator

| Check | Batch Creator | Manual Fallback |
|-------|--------------|-----------------|
| Against | ALL completed experiments (3,183) | RUNNING tasks only (9) |
| Threshold | Jaccard > 0.45 | Word overlap > 0.3 |
| Result | 0-1 uncovered | 22 uncovered |
| Purpose | "Has this question been answered?" | "Is someone already working on this?" |

The batch creator's check is valid for its purpose (preventing duplicate experiments on already-answered questions). But the Director's question is different: "Are free workers idle while unique queue items exist?" — which requires checking against running tasks only.

## Pitfall: Python `&` operator

When writing the scoring script to a file and running via terminal, `set_a & set_b` gets interpreted by bash as a background operator. Use `.intersection()` method instead.
