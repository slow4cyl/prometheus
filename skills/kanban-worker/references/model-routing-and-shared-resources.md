# Model Routing and Shared Resources

## Default Model: mimo-v2.5

**Use mimo-v2.5 for ALL experiments unless there is a specific reason to use a different model.**

- Cost: $0.14/M input, $0.28/M output
- Cached: $0.0028/M (97% cache hit rate)
- Why default: cheaper, faster, scores higher on benchmarks, no shared resource contention

**When to use a different model:**
- Testing a specific model's behavior (e.g., "test Qwen's reasoning patterns")
- Comparing models (e.g., "Qwen vs mimo vs DeepSeek on X task")
- Experiments where model identity IS the variable being tested
- When the experiment specifically requires features only available in a specific model

**The key question:** Does this experiment need THIS SPECIFIC MODEL, or does it just need good inference? If it just needs inference, use mimo-v2.5.

## Shared Resources Concept

Workers operate in isolation — they don't know about each other. When multiple workers share a resource (like a GPU server), each one acting in self-interest can destroy the shared resource.

**The tragedy of the commons pattern:**
1. Worker A sees a shared inference server and thinks "I'll optimize it for my experiment"
2. Worker A launches its own vLLM instance with different config
3. GPU memory fights, both instances crash
4. Workers B through V fall back to expensive API calls
5. $52 burned in 15 minutes

**The fix:** Clear rules about what workers can and can't do with shared resources. Rules must be in task descriptions, not just SKILL.md — workers generate code from task descriptions.

## Infrastructure Rules Are Machine-Specific

Infrastructure protection rules apply to SPECIFIC MACHINES, not globally. Workers can do whatever they need on their local machine — modify files, run scripts, install packages. The rules are only about the shared remote resource.

**Example:** "Don't SSH to the shared inference server" does NOT mean "don't SSH to anything." It means don't SSH to the specific machine that other workers depend on.

## Deleted Models = Cannot Launch

If workers shouldn't use a model, delete it from the machine. Workers can't launch what doesn't exist. This is more reliable than documentation alone.

**Example:** If only Qwen3.6-35B-A3B should run on the shared server, delete all other model directories. Workers trying to launch other models will OOM.
