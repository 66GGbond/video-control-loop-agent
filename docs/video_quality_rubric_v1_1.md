# Video Quality Rubric v1.1

This document is the executable companion to the product rubric. It is scoped to visual quality only: audio, dialogue, lip-sync, and music do not affect a release decision.

## Decision boundary

The visual model is an **observation tool**, not the release authority. It receives labelled keyframes and returns five diagnostic scores plus one observation for every planned acceptance criterion. Application code validates those observations and derives exactly one result:

1. `fail` — a critical criterion has evidence-backed failure, a blocking issue is present, or a diagnostic core score is below 3.
2. `insufficient_evidence` — any critical criterion lacks valid frame-level evidence. No repair task is created from a guess.
3. `pass` — all critical criteria have evidence-backed passes; there is no blocker; overall visual diagnostic score is at least 4 and core scores are at least 3.

`overall` is a diagnostic trend value only. It never overrides a critical failure.

## Inputs and evidence

The control planner produces 1–4 atomic acceptance criteria per planned semantic beat. Every existing beat (`S1` through `S6`, depending on the plan) must be covered. A criterion carries:

```json
{
  "requirement_id": "s2_action_complete",
  "shot_id": "S2",
  "source": "prompt|control_plan|reference_image",
  "requirement": "one visually testable requirement",
  "expected": "observable expected state",
  "critical": true,
  "asset_bindings": ["R1"]
}
```

Browser sampling gives each keyframe an ID, timestamp, expected shot, and reason. Sample count is adaptive: one baseline frame per shot plus extra frames around the plan's risk-focused moments. The current Demo samples 4–6 keyframes and sends compressed 512px images. This is evidence sampling, not a claim of full video coverage.

The Asset Manifest assigns ordered uploaded materials IDs such as `R1` and records their explicit `@` associations. A reference preview is included as a separate visual input only when it is an explicitly bound image and the criterion lists its matching `asset_bindings` ID. Uploading a reference alone may condition generation, but it is not evidence for a reference-specific quality claim.

## Visual observations

The model must return exactly one record for each supplied criterion:

```json
{
  "requirement_id": "s2_action_complete",
  "status": "pass|fail|insufficient_evidence",
  "observed": "only what visible frames support",
  "evidence": [
    {"frame_id": "F3", "timestamp": 2.14, "description": "The product is fully unfolded."}
  ],
  "repair_hint": "optional concise action for fail/insufficient_evidence"
}
```

The server discards evidence that does not cite a known frame or whose frame is not mapped to the criterion's planned shot. A `pass` or `fail` without valid evidence is converted to `insufficient_evidence`.

The five displayed diagnostic dimensions are: Prompt Alignment, Subject / Identity Consistency, Camera Control, Visual Quality, and Narrative Continuity. They are intentionally diagnostic summaries for this 2–3 day Demo; the atomic criteria enforce product integrity, action/physics, temporal continuity, and visual-artifact release blockers where relevant.

## Known scope

- Keyframes cannot prove unobserved time intervals. When temporal proof is required, increase targeted sampling or add short-video/scene analysis later.
- The current Demo does not evaluate audio.
- The VLM does not decide `release_decision`, `critical`, or the release policy. Those values remain code-controlled and auditable.
