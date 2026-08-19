# Agent Decision Policy

## Goal

Within the user-approved repair budget, satisfy the release policy while preserving stated character and story constraints.

## Available actions

| Action | Use when |
|---|---|
| `rewrite_prompt` | A direct instruction is unclear or competing with other text |
| `inject_continuity_state` | A later shot lost a known prop, wardrobe or emotional state |
| `add_reference_constraint` | Identity or important visual asset drifted |
| `split_shot` | A short clip contains a multi-step action chain; the next *complete clip* should express it as clearer consecutive micro-beats |
| `regenerate_shot` | The specification is sound but the result fails materially; the target shot is the evidence-backed priority within the next *complete clip* |
| `accept_and_continue` | Only non-blocking issues remain and repair value is low |
| `stop_and_report` | Budget is exhausted or no authorised repair is viable |

## Decision ordering

1. Never accept a blocking defect.
2. Repair the defect with the largest impact on story intent.
3. Prefer a targeted evidence priority and preservation set over an uncontrolled rewrite. The configured video provider still regenerates a complete clip; it is not a local shot editor.
4. Prefer a lower-risk task decomposition over repeating a failed complex action.
5. Stop automatically at the release gate or the budget boundary.

## What the LLM decides

The planner ranks Bad Cases, chooses the repair action, writes the task, and explains rejected options. Policies validate action legality, thresholds, and budget.
