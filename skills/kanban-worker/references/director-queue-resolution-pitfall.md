# Director Queue Item Resolution — Self_state.json Write Pitfall

Added: Cycle #217. Category: Director single-writer invariant violation (queue resolution variant).

## Problem

When the Director classifies queue items and determines some are RESOLVED, it may be tempted to write the [RESOLVED by exp_XXX] tag directly into self_state.json curiosity queue. This violates the single-writer invariant - only the synthesis worker writes to self_state.json.

This is distinct from the adding curiosities pitfall (director-queue-curiosity-pitfall.md). That pitfall covers the Director appending new items. This pitfall covers the Director modifying existing items (tagging RESOLVED).

## Fix

Instead of writing to self_state.json, add a kanban_comment to the synthesis task listing which items should be tagged RESOLVED and by which experiment.

## Prevention

During Director queue triage, when you identify RESOLVED items: (1) Note them in your local analysis, (2) Include them in a kanban_comment on the synthesis task, (3) Do NOT touch self_state.json.

Observed in Cycle #217: Director classified item 22 as RESOLVED by exp_701, wrote tag to self_state.json, realized violation, reverted, used kanban_comment instead.
