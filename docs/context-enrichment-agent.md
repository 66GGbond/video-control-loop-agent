# Context Enrichment Agent — MVP

This is the pre-generation Agent layer for Video Control Loop Agent. It improves the completeness of the approved input before DeepSeek produces a control plan; it does not replace the later quality-control loop.

## Goal

Maximise the coverage of high-impact, verifiable generation constraints while avoiding unsupported assumptions and unnecessary user interruption.

## Implemented tool boundary

| Tool | When it runs | Current behaviour |
|---|---|---|
| `calendar_context` | The brief explicitly requests seasonal/current-season/holiday context | Requires a target market when that context is high impact. Uses a local calendar adapter; it never claims live campaign data. |
| `reference_asset_analysis` | A reference image is present | Uses the configured visual model only when available. Otherwise the asset remains attached, but no visual feature is invented. |
| `capability_router` | Every new request | Chooses only an explicitly declared text-to-video or reference-conditioned route from the configured provider capability snapshot; blocks rather than guessing when no safe route exists. |
| `short_form_pattern_library` | A short-form request explicitly names a relevant platform or format | Declared but intentionally deferred. It makes no real-time trend claim. |

## Human boundary

If a seasonal request lacks a target market, the Agent enters `awaiting_context_confirmation`. The user supplies a market, then the Agent reruns enrichment and compiles the control plan. This is the only new top-level state; existing planning, generation, evaluation, repair, publish, and budget states remain unchanged.

## Facts passed downstream

The control planner receives the original brief plus `enriched_context`:

- `user_fact`: explicitly supplied information, such as market or asset role;
- `tool_fact`: versioned output from a registered Adapter;
- `model_inference`: advisory only, never promoted to a hard constraint automatically;
- `unknown`: preserved as unresolved rather than fabricated.

Only verified reference-image preservation features become immutable plan constraints. The later Prompt Compiler will use the same context to produce a model-specific generation prompt and an independent evaluation contract.
