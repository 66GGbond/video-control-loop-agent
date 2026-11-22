from __future__ import annotations

import uuid

from .conversation import messages
from .agent.orchestrator import AgentOrchestrator
from .agent.context import ContextEnrichmentAgent
from .agent.asset_manifest import build_asset_manifest
from .agent.planner import ArkControlPlanner, ArkRepairPlanner, FixturePlanner
from .agent.sampling import fallback_evidence_sampling_policy
from .agent.generation_spec import fallback_generation_spec, requested_duration_seconds
from .agent.duration_planning import build_duration_plan, fallback_beat_pacing
from .agent.prompt_compiler import PromptCompiler
from .domain.models import CreativeSpec, ProjectState, to_jsonable
from .tools.evaluator import ResilientVisionEvaluator


SAMPLE_PROMPT = (
    "A female detective with a black bob haircut and red trench coat discovers a silver "
    "necklace on a rainy neon street, picks it up, and reacts with relief."
)


def fallback_acceptance_criteria(shots: list[dict], constraints: list[str]) -> list[dict]:
    """Transparent fallback criteria used only when live planning is unavailable."""
    criteria = []
    for shot in shots:
        shot_id = shot["shot_id"]
        suffix = shot_id.lower()
        criteria.append({
            "requirement_id": f"{suffix}_intent_visible",
            "shot_id": shot_id,
            "source": "control_plan",
            "requirement": shot["intent"],
            "expected": "The planned subject, action, or outcome is visibly present.",
            "critical": True,
        })
    if constraints:
        criteria.append({
            "requirement_id": "cross_shot_constraints",
            "shot_id": shots[-1]["shot_id"],
            "source": "prompt",
            "requirement": "Preserve the user's non-negotiable constraints across the sequence.",
            "expected": "; ".join(constraints[:3]),
            "critical": True,
        })
    return criteria


def fallback_production_direction(shots: list[dict]) -> dict:
    """Constrained execution defaults for the clearly labelled local fallback."""
    action_present = any(item.get("beat_function") in {"action", "transition"} for item in shots)
    return {
        "source": "policy_default",
        "camera_language": "Use stable, readable framing and only introduce camera movement when it clarifies the planned action or outcome.",
        "visual_treatment": "Keep lighting, composition, and texture coherent with the submitted brief; preserve readable important details.",
        "motion_continuity": "Preserve subject position, screen direction, and important prop state through the planned action." if action_present else "Keep the approved visual state stable long enough for the planned detail to be read.",
        "negative_guards": [
            "Do not introduce unrequested people, products, logos, or events.",
            "Do not replace a required visual detail with decorative variation.",
        ],
    }


def create_empty_state() -> ProjectState:
    """A reset returns the user to the Brief stage, never to a hidden sample project."""
    spec = CreativeSpec(goal="", source_prompt="", immutable_character_attributes=[], shots=[], max_iterations=0)
    return ProjectState(project_id=uuid.uuid4().hex, creative_spec=spec, current_version_id="none", status="ready_for_brief")


def create_demo_state(
    prompt: str | None = None,
    reference_asset: dict | None = None,
    enriched_context: dict | None = None,
    multimodal_assets: list[dict] | None = None,
) -> ProjectState:
    prompt = (prompt or SAMPLE_PROMPT).strip()
    is_sample = prompt == SAMPLE_PROMPT
    brief_excerpt = " ".join(prompt.split())[:220]
    pacing = fallback_beat_pacing(prompt)
    sample_intents = [
        "Establish a rainy neon street and detective searching.",
        "Detective picks up a silver necklace from wet pavement.",
        "Detective holds the necklace and reacts with relief.",
    ]
    shots = []
    for index, beat in enumerate(pacing):
        function = beat["function"]
        intent = sample_intents[index] if is_sample and index < len(sample_intents) else {
            "detail": f"Present the requested subject, setting, and visual style from this brief: {brief_excerpt}",
            "establish": f"Establish the requested subject, setting, and visual style from this brief: {brief_excerpt}",
            "action": f"Make the brief's central action visibly complete in one controlled, observable beat: {brief_excerpt}",
            "transition": "Carry the required visual state into the next planned beat without breaking continuity.",
            "payoff": "Show the requested outcome while preserving the subject, setting, and stated constraints.",
        }[function]
        shots.append({"shot_id": beat["shot_id"], "intent": intent, "beat_function": function, "pacing": beat["pacing"]})
    for index, shot in enumerate(shots):
        shot.update({
            "timing_reason": "Local recovery pacing based on the submitted brief; review this plan before generation.",
            "critical_moment": (
                "Verify the required subject, setting, and initial visual state."
                if index == 0 else
                "Verify the required final state and carried-forward continuity."
                if index == len(shots) - 1 else
                "Verify the planned action or transition is visibly completed."
            ),
        })
    duration_plan = build_duration_plan(
        shots,
        explicit_duration_seconds=requested_duration_seconds(prompt),
        source="deterministic_recovery",
        rationale="A bounded local recovery plan derived a minimal set of semantic beats from the submitted brief.",
    )
    shots = duration_plan["timeline"]
    generation_spec = fallback_generation_spec(
        prompt, reference_asset, duration_override=duration_plan["total_duration_seconds"],
    )
    recommended_iterations = 2 if is_sample or len(prompt) > 120 or reference_asset else 1
    rationale = (
        "The brief includes a multi-step action and cross-shot prop continuity. Two repair rounds "
        "allow one targeted repair and one verification pass."
        if recommended_iterations == 2
        else "The brief has one primary action and limited continuity risk. One targeted repair round is recommended."
    )
    spec = CreativeSpec(
        goal=prompt,
        source_prompt=prompt,
        immutable_character_attributes=(
            ["black bob haircut", "red trench coat", "female detective"]
            if is_sample else [f"User brief: {brief_excerpt}", "Preserve the requested subject identity and visual style", "Keep the key action and scene context continuous"]
        ),
        shots=shots,
        max_iterations=recommended_iterations,
        reference_asset=reference_asset,
        multimodal_assets=multimodal_assets or ([reference_asset] if reference_asset else []),
        recommended_iterations=recommended_iterations,
        planning_rationale=rationale,
        risk_forecast=(
            ["Multi-step action completion", "Cross-shot prop continuity", "Character and wardrobe consistency"]
            if is_sample
            else ["Primary subject consistency", "Key action completion", "Cross-shot scene and constraint continuity"]
        ),
        evidence_sampling=fallback_evidence_sampling_policy(
            shots,
            ["Multi-step action completion", "Cross-shot prop continuity", "Character and wardrobe consistency"]
            if is_sample
            else ["Primary subject consistency", "Key action completion", "Cross-shot scene and constraint continuity"],
        ),
        generation_spec=generation_spec,
        duration_plan=duration_plan,
        production_strategy={"mode": "single_clip", "reason": duration_plan["reason"]},
        production_direction=fallback_production_direction(shots),
        acceptance_criteria=fallback_acceptance_criteria(
            shots,
            ["black bob haircut", "red trench coat", "female detective"] if is_sample else [f"User brief: {brief_excerpt}", "Preserve the requested subject identity and visual style", "Keep the key action and scene context continuous"],
        ),
        enriched_context=enriched_context or {},
        planning_source="fallback",
        planning_notice="A local recovery plan was built from the submitted brief after the live planner could not return a valid plan. Review the brief-derived shots before approval.",
    )
    state = ProjectState(project_id=uuid.uuid4().hex, creative_spec=spec, current_version_id="none", status="planning_ready")
    return PromptCompiler().compile(state)


def create_project_state(
    prompt: str,
    reference_asset: dict | None = None,
    control_planner: ArkControlPlanner | None = None,
    enriched_context: dict | None = None,
    multimodal_assets: list[dict] | None = None,
) -> ProjectState:
    """Use the live planning tool when available, with a deterministic fallback."""
    def finish(state: ProjectState) -> ProjectState:
        context = enriched_context or {}
        state.creative_spec.enriched_context = context
        state.creative_spec.multimodal_assets = multimodal_assets or ([reference_asset] if reference_asset else [])
        state.creative_spec.asset_manifest = context.get("asset_manifest") or build_asset_manifest(state.creative_spec.multimodal_assets)
        # Only visual facts produced by a configured tool are copied into the
        # plan.  Inferences and unknowns stay visible in context, never become
        # non-negotiable generation constraints by accident.
        additions = [
            str(item.get("text")).strip()
            for item in context.get("constraints", [])
            if isinstance(item, dict) and item.get("source") == "tool_fact" and item.get("strength") == "hard" and str(item.get("text", "")).strip()
        ]
        for item in additions:
            if item not in state.creative_spec.immutable_character_attributes:
                state.creative_spec.immutable_character_attributes.append(item)
        state.creative_spec.immutable_character_attributes = state.creative_spec.immutable_character_attributes[:8]
        strategy = state.creative_spec.production_strategy or {"mode": "single_clip"}
        if strategy.get("mode") == "needs_segmentation":
            state.status = "needs_segmentation"
            state.creative_spec.planning_notice = (
                "The Agent kept this request out of the single-clip generator because it needs a multi-segment production plan. "
                f"{strategy.get('reason', '')}"
            ).strip()
            state.history.append({
                "at": "local-demo", "phase": "reason", "event": "segmentation_required",
                "payload": {"message": state.creative_spec.planning_notice},
            })
            return state
        return PromptCompiler().compile(state)
    if control_planner and control_planner.available():
        try:
            spec = control_planner.plan(prompt, reference_asset, enriched_context)
            return finish(ProjectState(project_id=uuid.uuid4().hex, creative_spec=spec, current_version_id="none", status="planning_ready"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            fallback = create_demo_state(prompt, reference_asset, enriched_context, multimodal_assets)
            failure_kind = type(exc).__name__
            fallback.creative_spec.planning_notice = (
                "Doubao Seed 2.1 Pro did not return a valid executable control plan for this request "
                f"({failure_kind}); the Agent is showing a clearly labelled local recovery plan instead."
            )
            fallback.history.append({
                "at": "local-demo",
                "phase": "observe",
                "event": "control_planner_fallback",
                "payload": {
                    "planner": "doubao_seed_2_1_pro",
                    "failure_kind": failure_kind,
                    "message": "The live planner was unavailable or failed control-plan validation; a deterministic recovery plan was used.",
                },
            })
            return finish(fallback)
    fallback = create_demo_state(prompt, reference_asset, enriched_context, multimodal_assets)
    fallback.creative_spec.planning_notice = "A local recovery plan was built from the submitted brief because no live planning model is available to the local server."
    return finish(fallback)


def create_enriched_project_state(
    prompt: str,
    reference_asset: dict | None = None,
    control_planner: ArkControlPlanner | None = None,
    context_agent: ContextEnrichmentAgent | None = None,
    target_market: str = "",
    multimodal_assets: list[dict] | None = None,
    ui_language: str = "zh",
) -> ProjectState:
    """Run the bounded pre-generation agent before compiling a control plan."""
    agent = context_agent or ContextEnrichmentAgent()
    context = agent.enrich(prompt, reference_asset, target_market, multimodal_assets)
    context["ui_language"] = "en" if ui_language == "en" else "zh"
    if context["status"] == "needs_user_confirmation":
        spec = CreativeSpec(
            goal=prompt,
            source_prompt=prompt,
            immutable_character_attributes=[],
            shots=[],
            max_iterations=0,
            reference_asset=reference_asset,
            multimodal_assets=multimodal_assets or ([reference_asset] if reference_asset else []),
            enriched_context=context,
            planning_source="context_enrichment",
            planning_notice="The Agent needs one confirmed context field before it can compile a reliable control plan.",
        )
        state = ProjectState(project_id=uuid.uuid4().hex, creative_spec=spec, current_version_id="none", status="awaiting_context_confirmation")
    else:
        state = create_project_state(prompt, reference_asset, control_planner, context, multimodal_assets)
    for observation in context.get("tool_observations", []):
        state.history.append({
            "at": "local-demo",
            "phase": "context_tool",
            "event": observation.get("tool", "context_tool"),
            "payload": {"message": observation.get("result", {}).get("summary", observation.get("status", "completed"))},
        })
    if context.get("open_questions"):
        state.history.append({
            "at": "local-demo",
            "phase": "reason",
            "event": "context_confirmation_requested",
            "payload": {"message": context["open_questions"][0].get("why", "The Agent needs a high-impact context field.")},
        })
    return state


def create_orchestrator() -> AgentOrchestrator:
    fallback_planner = FixturePlanner()
    live_planner = ArkRepairPlanner()
    planner = live_planner if live_planner.available() else fallback_planner
    return AgentOrchestrator(ResilientVisionEvaluator(), planner, fallback_planner, PromptCompiler())


def present(state: ProjectState) -> dict:
    """A UI-safe projection. Full state remains serialisable for future persistence."""
    state_json = to_jsonable(state)
    state_json["conversation"] = messages(state)
    # Frame files are local-only evaluator inputs; never expose filesystem paths to the browser.
    state_json.pop("evaluation_frame_records", None)
    for review in state_json.get("version_reviews", []):
        if isinstance(review.get("asset"), dict):
            review["asset"].pop("local_video_path", None)
    if isinstance(state_json.get("current_video_asset"), dict):
        state_json["current_video_asset"].pop("local_video_path", None)
    # Reference media are submitted server-to-provider only. The client needs
    # names, roles, and image previews, but should never receive audio Base64
    # or duplicate full-resolution media from the state endpoint.
    spec = state_json.get("creative_spec")
    if isinstance(spec, dict):
        for asset in spec.get("multimodal_assets", []) or []:
            if isinstance(asset, dict) and asset.get("media_type") == "audio":
                asset.pop("data_url", None)
    # Dataclass serialization preserves score fields but not the `overall()`
    # method. The browser receives this explicit diagnostic value; audio is
    # deliberately excluded by the visual-only rubric.
    reports = [state_json.get("latest_report"), *(state_json.get("evaluation_history") or [])]
    reports.extend(item.get("report") for item in state_json.get("version_reviews", []) if item.get("report"))
    for report in reports:
        scores = report.get("scores") if isinstance(report, dict) else None
        if not isinstance(scores, dict):
            continue
        diagnostic_overall = report.get("diagnostic_overall") if isinstance(report, dict) else None
        if isinstance(diagnostic_overall, (int, float)) and diagnostic_overall > 0:
            scores["overall"] = round(float(diagnostic_overall), 2)
            continue
        visual_scores = [value for key, value in scores.items() if key != "audio_visual_alignment" and isinstance(value, (int, float)) and not isinstance(value, bool)]
        if visual_scores:
            scores["overall"] = round(sum(visual_scores) / len(visual_scores), 2)
    return state_json
