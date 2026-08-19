# 5-Minute Product Demo Script

## Aim

Show that the product is an Agentic quality-control system for AI video, rather than a prompt wrapper or a fixed regeneration workflow.

Use one short brief with an obvious controllability requirement. A good example is: a branded product performs one clear action, maintains its appearance, and ends in a defined final state. Use a prepared run only if live video generation latency is unpredictable; say so openly.

## 0:00–0:35 — Start with the user problem

> “Video generation often fails in a frustrating way: a clip may look attractive, but still miss the product shape, key action, or continuity requirement. Creators then retry blindly. This demo treats every retry as a decision that needs evidence, a budget, and an explanation.”

Point to the initial Project Status and the Creative Agent. Do not begin by describing models or code.

## 0:35–1:10 — Show the Brief and bounded context

1. Enter the brief and optionally attach ordered image and audio references. Use `@` to associate a material with a stated requirement when reference-specific evaluation matters.
2. If the brief contains a seasonal claim, show the Agent asking for the target market. Explain that it asks only because this fact could change the generation plan.
3. Expand **Agent execution trace**.

> “The Agent does not silently invent missing context. Tool observations are separated from user facts, and unsupported inferences cannot become hard constraints.”

## 1:10–1:50 — Explain the Control Plan and Prompt Compiler

Show the 1–6 semantic-beat plan, immutable constraints, acceptance criteria, controllability risks, Asset Manifest, and recommended repair budget.

> “The key output is not a prettier prompt. It is two linked contracts: one tells a generation provider what to create; the other tells the evaluator what evidence must be collected to accept it. The same constraint is therefore traceable across generation and evaluation.”

Mention the user can adjust the repair maximum or choose Agent autonomy. Explain that changing budget pauses autonomy until the user resumes it.

## 1:50–2:25 — Demonstrate tool routing rather than fixed workflow

Approve the plan and generate V1. Expand the execution trace after the provider route appears.

> “The Capability Registry selects only an explicitly declared valid route. A reference image is not automatically passed to a video model: if the configured adapter has not verified reference-image support, the Agent chooses text-to-video and logs why it rejected the risky route.”

> “That lets us switch providers later without rewriting the quality loop.”

## 2:25–3:20 — Observe evidence, not a generic score

After generation, run the quality loop. Show the sampling progress, then the selected version in **Version review**.

> “The Agent samples frames according to the accepted plan and risk forecast. It is not randomly looking at six frames. For an action it needs before/during/after evidence; for a product it compares first and final product integrity.”

Show **Bad Case evidence** and the Quality Rubric.

> “The overall number is diagnostic only. The release gate needs frame-cited evidence for every critical requirement and a minimum score on each active visual dimension. A good-looking clip cannot pass if the key action is unsupported.”

## 3:20–4:20 — Reason and act within a budget

Show **Agent decision** and the decision log.

> “The planner chooses the smallest justified *change set* and records rejected alternatives. With the current provider, execution is always a new complete clip: S2 is the evidence-backed priority, not a claim that the model locally edits only S2. For example, it can decompose an impossible multi-action beat instead of spending another attempt on the same ambiguous prompt.”

Generate the repair, or switch to the prepared V2. State clearly if it is prepared rather than live.

## 4:20–5:00 — Verify and close with product judgment

Switch versions to show that V1’s report does not change when V2 is evaluated. Show the trajectory and the release decision.

> “Version evidence is immutable. If quality passes, the Agent stops; if the budget is exhausted, it asks for human approval rather than silently spending more. The core product decision is that controllability needs an auditable loop: plan, evidence, bounded repair, and verified release.”

End with the execution trace:

> “This is the line I would continue to develop: provider-agnostic generation routing, stronger multimodal evidence—including audio—and Bad Case learning across projects, while retaining human control over cost and brand constraints.”

## Design discussion prompts

| Question | Concise answer |
|---|---|
| Why is this an Agent? | It chooses tools and next actions from state and evidence, can stop or ask for approval, and retains memory across versions. A fixed pipeline would always run the same steps. |
| Why not let the LLM decide everything? | Model judgment is useful for plan and repair ranking; deterministic policy should own budget, evidence validity, and release gates. |
| Why sample frames instead of assess the whole video? | It is a practical MVP boundary. The sampling plan is derived from the control plan and risks. A future adapter can replace it with native video/audio understanding. |
| Why keep providers abstract? | Video-model capabilities change quickly. The product’s durable value is quality control and decision logic, while model modes belong in verified provider adapters. |
| What would you validate next? | Three real-video cases: product integrity, character continuity, and action completion; then calibrate the rubric and repair-success rate from evidence. |
