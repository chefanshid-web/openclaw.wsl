import os

target_dir = "/home/dell/.openclaw/workspace/skills/lazy"
target_file = f"{target_dir}/SKILL.md"

os.makedirs(target_dir, exist_ok=True)

content = """---
name: lazy
description: Evaluate pasted content and say only whether it adds value. Triggered by /lazy.
---

# Lazy Filter

## What this does
Evaluates any pasted content against the project doctrine and returns a five-line verdict.

## When to use
Triggered by `/lazy` followed by any content.

## Steps
1. Read the pasted content.
2. Ask: does it make the system more honest, or less likely to blow up?
3. If yes: identify where it fits and what it replaces.
4. If no: reply "Skip."
5. Output exactly five lines, no more.

## Output format
1. Keep or Skip
2. One-sentence reason
3. Where it fits (or "nowhere")
4. What it replaces (or "nothing")
5. One-line action (or "ignore")

## Rules
- No essays. No buckets. No re-architecture.
- Default answer is Skip.
- If nothing useful: reply "skip it" and stop.
- Never propose new agents, new layers, or new phases.

## Never
- Add complexity.
- Suggest tools that cost money.
- Override the doctrine.
- Produce anything longer than five lines.
"""

with open(target_file, "w", encoding="utf-8") as f:
    f.write(content)

print("File written successfully.")
