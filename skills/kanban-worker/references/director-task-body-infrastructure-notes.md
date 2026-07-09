# Director Task Body Infrastructure Notes — Required vs Optional (Cycle #229)

## Problem

Director-created experiment tasks include a standard infrastructure routing note. When the endpoint is unreachable, workers interpret this as a hard requirement and block — even for experiments that don't need Qwen.
When the endpoint is unreachable, workers interpret this as a hard requirement and block — even for experiments that don't need Qwen.

## Pattern

In Cycle #229, 4 out of 5 new tasks blocked on endpoint being unreachable. Only 2 actually needed Qwen (attention patterns, calibration scores). The other 3 (dialogue FPR, GDPR terminology, word_count analysis) could run with mimo/deepseek.

## Fix for Director

When creating experiment tasks, explicitly state MODEL REQUIREMENTS in the task body:

For experiments that REQUIRE Qwen:
```
MODEL REQUIREMENTS: THIS EXPERIMENT REQUIRES Qwen3.6-35B-A3B.
If the Qwen endpoint is unreachable, BLOCK immediately.
```

For experiments that work with any model:
```
MODEL REQUIREMENTS: This experiment works with mimo-v2.5 and deepseek-v4-flash.
Use OpenRouter for these models. Qwen is optional for additional data points.
```

## Key Insight

The word "MUST" in infrastructure notes is interpreted as a task requirement by workers. Changing the framing from "MUST use" to "IF using, here's how" prevents over-blocking.
