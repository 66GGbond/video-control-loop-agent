from __future__ import annotations

import uuid

from ..domain.models import Action, ContinuityState, Decision, EvaluationReport, ProjectState, RepairTask


def build_continuity_memory(state: ProjectState, report: EvaluationReport) -> list[ContinuityState]:
    """Record only plan-derived continuity rules; do not invent a sample character or prop."""
    constraints = "; ".join(state.creative_spec.immutable_character_attributes[:4]) or "Preserve the approved subject and visual constraints."
    affected = ", ".join(sorted({dimension for case in report.bad_cases for dimension in case.affected_dimensions}))
    memory: list[ContinuityState] = []
    for shot in state.creative_spec.shots:
        memory.append(ContinuityState(
            shot_id=shot["shot_id"],
            character_state=f"Preserve approved constraints: {constraints}",
            prop_state="Maintain any visible product, object, and state changes across adjacent shots.",
            setting_state=f"Shot intent: {shot['intent']}",
            emotional_state=f"Quality dimensions under review: {affected or 'all release dimensions'}.",
        ))
    return memory


def rewrite_generation_task(state: ProjectState, decision: Decision) -> RepairTask:
    target_shot = decision.target_shot or (state.creative_spec.shots[0]["shot_id"] if state.creative_spec.shots else "S1")
    shot = next((item for item in state.creative_spec.shots if item["shot_id"] == target_shot), None)
    target_intent = shot["intent"] if shot else "Fulfil the approved target-shot intent."
    original_brief = state.creative_spec.source_prompt or state.creative_spec.goal
    constraints = state.creative_spec.immutable_character_attributes or ["Preserve the approved subject, setting, and style."]
    user_direction = state.pending_user_direction.strip()
    direction_clause = f" User-directed refinement for this next version: {user_direction}." if user_direction else ""
    report = state.latest_report
    failed_check = next(
        (
            check for check in (report.atomic_checks if report else [])
            if check.get("shot_id") == target_shot and check.get("status") == "fail"
        ),
        None,
    )
    if failed_check:
        evidence = (
            f"Requirement: {failed_check.get('requirement', '')}. Expected: {failed_check.get('expected', '')}. "
            f"Observed: {failed_check.get('observed', '')}."
        )
    else:
        evidence = next((case.evidence for case in (report.bad_cases if report else []) if case.shot_id == target_shot), "Address the observed quality failure in this shot.")
    if decision.action == Action.SPLIT_SHOT:
        prompt = (
            f"Regenerate one complete video clip containing two concise consecutive micro-beats that fulfil this approved intent: {target_intent} "
            "Use the first micro-shot to establish the action state and the second to make the result visibly complete. "
            f"Original creative brief: {original_brief}. Keep these constraints unchanged: {'; '.join(constraints)}."
            f"{direction_clause}"
        )
        constraints = [
            f"Approved target: {target_intent}",
            f"Observed evidence to address: {evidence}",
            *[f"Immutable: {item}" for item in state.creative_spec.immutable_character_attributes],
            "Carry visible subject, product, and scene state forward into the next shot.",
            *([f"User-directed refinement: {user_direction}"] if user_direction else []),
        ]
        handoff = (
            f"Regenerate the complete clip with two concise micro-beats around {target_shot}, "
            "then re-evaluate the new version. This is not a local shot edit."
        )
    else:
        prompt = (
            f"Regenerate the complete video clip. Prioritise {target_shot} to fulfil: {target_intent}. "
            f"Address this observation: {evidence}. "
            f"Optimisation direction: {decision.reason}. "
            f"Original creative brief: {original_brief}. Keep unchanged: {'; '.join(constraints)}."
            f"{direction_clause}"
        )
        constraints = [f"Approved target: {target_intent}", f"Observed evidence to address: {evidence}", *[f"Immutable: {item}" for item in constraints], *([f"User-directed refinement: {user_direction}"] if user_direction else [])]
        handoff = (
            f"Regenerate the complete clip with {target_shot} as the evidence-backed repair priority, "
            "then re-evaluate the new version. Passed requirements remain preservation constraints."
        )

    return RepairTask(
        task_id=f"repair-{uuid.uuid4().hex[:8]}",
        action=decision.action,
        target_shot=target_shot,
        prompt=prompt,
        continuity_constraints=constraints,
        human_handoff=handoff,
        execution_scope="whole_clip_regeneration",
        scope_notice=(
            f"The configured provider uses complete-clip regeneration. {target_shot} is the diagnostic target, "
            "not a provider-side local-edit operation."
        ),
    )
