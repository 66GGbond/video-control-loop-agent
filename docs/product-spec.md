# Product Spec — Video Control Loop Agent

## Problem

AI-video creators often react to unusable output by regenerating blindly. They do not know whether a failure is caused by an overlong action, missing continuity constraint, inadequate reference asset, or an incompatible shot request.

## Target user

A creator producing a short AI video who needs the result to follow an approved story, product, character, or multimodal-reference specification.

## Job to be done

When a generated video does not meet my creative intent, help me identify the blocking defect and choose the most justified next complete-clip regeneration before I spend another generation attempt.

## Product decision

The MVP converts a brief into 1–6 semantic beats and optimises for controllability, not aesthetic novelty. The system is successful when it identifies the right blocking failure, chooses an appropriate regeneration priority, and exposes the evidence, model boundary, and cost trade-off to the user. A target beat is a diagnostic focus; current providers regenerate the complete clip rather than locally editing one shot.

## Success signals for a demo

- A reviewer can explain why S2, not S1, is the priority inside the next complete-clip regeneration.
- Every planner action cites evidence and a constrained tool.
- The repaired version has a measurable improvement.
- The system stops when the quality policy says it should.
