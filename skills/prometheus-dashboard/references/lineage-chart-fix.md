# Lineage Chart Fix (2026-06-30)

## Problem

The "Active Curiosity Queue by Depth" bar chart in the Lineage tab was unruly:

1. **197 bars in ~1100px**: The data had 197 distinct depth levels (0-198). CSS `.depth-bar { min-width:8px }` meant bars couldn't shrink below 8px, so 197 × 8 = 1576px overflowed the panel. `.panel { overflow:hidden }` clipped them silently.

2. **Linear scale dominated by one bucket**: Depth 1 had 6242 curiosities; most depths had 50-300. At linear scale, one bar was full height and 196 were sub-pixel nubs.

3. **Label overlap**: 197 depth labels (9px font) in 1100px = unreadable pile. Same for the count labels on top.

## Data Shape

Queried from `~/.hermes/prometheus.db`:
```sql
SELECT evidence_depth, COUNT(*) as cnt
FROM curiosities WHERE status='active'
GROUP BY evidence_depth ORDER BY evidence_depth
```

Result: 198 rows, depths 0-198 (2 gaps: 188 and 197 missing).
- Depth 0: 2189, Depth 1: 6242 (peak), Depth 2: 2036
- Mid-range (10-99): 150-451 per depth
- Tail (100-198): 1-102 per depth
- Total: 28684 active curiosities

## Fix Applied

### Bucketing
Depths grouped into ranges of 10 (0-9, 10-19, ..., 190-199). 197 bars → 20 bars. Each bucket aggregates:
- `count`: sum of all depths in range
- `max_count` / `max_depth`: the single deepest peak within the bucket (for tooltip)

### Sqrt scale
Bar height = `math.sqrt(count) / math.sqrt(max_bucket_count) * 100`

This compresses the dominant 0-9 bucket (12809) from 100% at linear to 100% at sqrt (still tallest), while lifting the tail buckets (160-199, counts 50-76) from ~0.4% to ~6-8% — visible but still ordered.

Heights after fix:
```
0-9:      12809 → 100%
10-19:     2342 →  43%
20-29:     2606 →  45%
...
160-169:     67 →   7%
190-199:     51 →   6%
```

### CSS changes
- `min-width: 8px → 18px` (fewer bars, can be wider)
- `gap: 2px → 3px`
- `height: 120px → 140px`
- Added `overflow-x:auto` on chart container (fallback if bars still overflow)
- Added `padding-bottom: 28px` for label space
- Added `white-space:nowrap` on labels/counts
- Added `:hover { filter:brightness(1.3) }` for interactivity

### Tooltip
Each bar shows: `depth 0-9: 12809 active (peak at d1: 6252)`

## Code Location

- CSS: lines ~1107-1112 in `~/prometheus_dashboard_v2.py`
- Python: `render_lineage()` function, lines ~1437+

## Lesson

When a bar chart has (a) too many bars for the container and (b) a dominant outlier, fix both: bucket to reduce bar count, and use sqrt/log scale to make the tail visible. Don't just fix one — each problem compounds the other.
