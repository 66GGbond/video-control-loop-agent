"""Prompt Compiler v1.1 for the Video Control Loop Agent.

This module is deliberately provider-agnostic.  It turns an approved creative
plan into two auditable contracts:

* a generation contract the provider adapter can translate into a request;
* an evaluation contract that ties sampling and quality checks back to the
  very same control-plan requirements.

The compiler never invents creative facts.  Its validation gates stop an
incomplete plan from silently becoming a generation request.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable

from ..domain.models import ProjectState, RepairTask
from ..domain.rubric import audio_evaluation_plan, dimension_for_criterion, select_rubric_profile
from .generation_spec import fallback_generation_spec, validate_generation_spec
from .duration_planning import duration_plan_from_timeline
from .sampling import bind_sampling_to_timeline, fallback_evidence_sampling_policy, validate_evidence_sampling_policy
from .timing import normalise_shot_timeline
from .video_capabilities import VideoCapabilityRegistry
from .asset_manifest import bind_criteria_to_assets, build_asset_manifest


class PromptCompiler:
    """Compile one control plan into generation and evaluation contracts."""

    schema_version = "1.1"

    def __init__(self, capability_registry: VideoCapabilityRegistry | None = None) -> None:
        self.capabilities = capability_registry or VideoCapabilityRegistry()

    def compile(
        self,
        state: ProjectState,
        repair_task: RepairTask | None = None,
        target_version_id: str | None = None,
    ) -> ProjectState:
        spec = state.creative_spec
        if not spec.goal or not spec.shots:
            raise ValueError("Prompt Compiler needs an approved objective and shot plan")
        output = validate_generation_spec(spec.generation_spec) if spec.generation_spec else fallback_generation_spec(
            spec.source_prompt or spec.goal, spec.reference_asset
        )
        strategy = spec.production_strategy or {"mode": "single_clip"}
        if strategy.get("mode") != "single_clip":
            raise ValueError("Prompt Compiler cannot submit a plan that requires multi-segment production")
        # The timing plan is the bridge between semantic beat planning and a
        # single provider-level video duration. It is also the source of truth
        # for evidence windows below.
        timeline = normalise_shot_timeline(spec.shots, output["duration_seconds"])
        spec.shots = timeline
        duration_plan = spec.duration_plan or duration_plan_from_timeline(timeline, output["duration_seconds"])
        duration_plan["timeline"] = [dict(shot) for shot in timeline]
        duration_plan["total_duration_seconds"] = output["duration_seconds"]
        spec.duration_plan = duration_plan
        rubric_profile = select_rubric_profile(spec)
        policy = bind_sampling_to_timeline(self._sampling_policy(spec, rubric_profile), timeline)
        audio_plan = audio_evaluation_plan(spec)
        provider_binding = self._provider_binding(spec, output)
        asset_manifest = build_asset_manifest(spec.multimodal_assets or ([spec.reference_asset] if spec.reference_asset else []))
        spec.asset_manifest = asset_manifest
        target_version = target_version_id or ("v1" if state.current_version_id == "none" else state.current_version_id)
        control_plan_id = spec.control_plan_id or self._stable_id("control", {
            "project": state.project_id,
            "source_prompt": spec.source_prompt,
            "shots": spec.shots,
            "constraints": spec.immutable_character_attributes,
            "production_direction": spec.production_direction,
            "criteria": spec.acceptance_criteria,
        })
        repair_delta = self._repair_delta(state, repair_task)
        compile_run_id = self._stable_id("compile", {
            "control_plan_id": control_plan_id,
            "target_version": target_version,
            'skill_execution':spec.enriched_context.get('skill_execution',{}).get('prompt_sha256'),
            "repair_delta": repair_delta,
            "output": output,
            "duration_plan": duration_plan,
            "rubric_profile_id": rubric_profile["profile_id"],
            "audio_evaluation_mode": audio_plan["mode"],
            "capability_profile_id": provider_binding["capability_profile"]["profile_id"],
        })
        generation_contract_id = self._stable_id("generation", {"compile_run_id": compile_run_id})
        evaluation_contract_id = self._stable_id("evaluation", {"compile_run_id": compile_run_id})
        trace = self._constraint_trace(spec, repair_delta)
        generation_contract = {
            "schema_version": self.schema_version,
            "generation_contract_id": generation_contract_id,
            "compile_run_id": compile_run_id,
            "control_plan_id": control_plan_id,
            "target_version_id": target_version,
            'skill_execution':dict(spec.enriched_context.get('skill_execution',{})),
            "objective": spec.goal,
            "shot_plan": [dict(shot) for shot in timeline],
            "duration_plan": duration_plan,
            "immutable_constraints": list(spec.immutable_character_attributes),
            "production_direction": dict(spec.production_direction),
            "output_spec": output,
            "asset_manifest": asset_manifest,
            "reference_strategy": self._reference_strategy(spec, provider_binding),
            "repair_delta": repair_delta,
            "constraint_trace": trace,
            "provider_binding": provider_binding,
        }
        criteria = self._criteria_with_trace(spec.acceptance_criteria, trace, rubric_profile)
        criteria = bind_criteria_to_assets(criteria, asset_manifest)
        criteria.extend(self._quality_policy_criteria(timeline, rubric_profile))
        criteria.extend(self._audio_presence_criteria(audio_plan, timeline[0]["shot_id"]))
        evaluation_contract = {
            "schema_version": self.schema_version,
            "evaluation_contract_id": evaluation_contract_id,
            "compile_run_id": compile_run_id,
            "control_plan_id": control_plan_id,
            "generation_contract_id": generation_contract_id,
            "target_version_id": target_version,
            "rubric_profile": rubric_profile,
            "acceptance_criteria": criteria,
            "audio_evaluation": audio_plan,
            "asset_manifest": asset_manifest,
            "evidence_plan": {
                "sampling_policy": policy,
                "coverage_rules": {
                    "required_shot_ids": [shot["shot_id"] for shot in timeline],
                    "critical_requirements_need_cited_frame_evidence": True,
                    "unseen_content_policy": "insufficient_evidence",
                },
            },
            "repair_delta": repair_delta,
        }
        self.validate(generation_contract, evaluation_contract)
        previous_compile_run_id = spec.compile_run_id
        spec.control_plan_id = control_plan_id
        spec.compile_run_id = compile_run_id
        spec.generation_contract = generation_contract
        spec.evaluation_contract = evaluation_contract
        spec.rubric_profile = rubric_profile
        # The browser's local frame extractor consumes the same policy as the
        # evaluator contract. This avoids a hidden, divergent sampling policy.
        spec.evidence_sampling = policy
        if previous_compile_run_id != compile_run_id:
            self._record(state, "prompt_compiled", {
                "compile_run_id": compile_run_id,
                "generation_contract_id": generation_contract_id,
                "evaluation_contract_id": evaluation_contract_id,
                "control_plan_id": control_plan_id,
                "target_version": target_version,
                "mode": generation_contract["provider_binding"]["intended_mode"],
                "rubric_profile": rubric_profile["profile_id"],
                "repair": bool(repair_task),
            })
        return state

    def validate(self, generation_contract: Dict[str, Any], evaluation_contract: Dict[str, Any]) -> None:
        """Run the MVP contract gates described in the product specification."""
        ids = [
            generation_contract.get("compile_run_id"),
            generation_contract.get("generation_contract_id"),
            generation_contract.get("control_plan_id"),
            evaluation_contract.get("evaluation_contract_id"),
        ]
        if not all(isinstance(item, str) and item for item in ids):
            raise ValueError("Prompt Compiler gate G9 failed: trace IDs are required")
        if evaluation_contract.get("compile_run_id") != generation_contract["compile_run_id"]:
            raise ValueError("Prompt Compiler gate G9 failed: contracts have different compile runs")
        if evaluation_contract.get("generation_contract_id") != generation_contract["generation_contract_id"]:
            raise ValueError("Prompt Compiler gate G9 failed: evaluation is not bound to generation")
        if evaluation_contract.get("asset_manifest") != generation_contract.get("asset_manifest"):
            raise ValueError("Prompt Compiler gate G4 failed: generation and evaluation must share an asset manifest")
        for entry in generation_contract.get("asset_manifest", []):
            if not isinstance(entry, dict) or not entry.get("asset_id") or entry.get("media_type") not in {"image", "audio"}:
                raise ValueError("Prompt Compiler gate G4 failed: asset manifest is malformed")
        constraints = generation_contract.get("immutable_constraints")
        trace = generation_contract.get("constraint_trace")
        if not isinstance(constraints, list) or not constraints or not isinstance(trace, list):
            raise ValueError("Prompt Compiler gate G1 failed: immutable constraints need trace entries")
        traced = {item.get("constraint") for item in trace if isinstance(item, dict) and "immutable_constraints" in item.get("generation_fields", [])}
        if any(item not in traced for item in constraints):
            raise ValueError("Prompt Compiler gate G1 failed: an immutable constraint is unmapped")
        ordered_shot_ids = [item.get("shot_id") for item in generation_contract.get("shot_plan", []) if isinstance(item, dict)]
        shot_ids = set(ordered_shot_ids)
        criterion_shots = {item.get("shot_id") for item in evaluation_contract.get("acceptance_criteria", []) if isinstance(item, dict)}
        if not 1 <= len(ordered_shot_ids) <= 6 or len(ordered_shot_ids) != len(shot_ids) or ordered_shot_ids != [f"S{index}" for index in range(1, len(ordered_shot_ids) + 1)] or not shot_ids.issubset(criterion_shots):
            raise ValueError("Prompt Compiler gate G2 failed: each planned shot needs an evaluation criterion")
        output = validate_generation_spec(generation_contract.get("output_spec"))
        expected_timeline = normalise_shot_timeline(generation_contract.get("shot_plan", []), output["duration_seconds"], strict=True)
        if expected_timeline != generation_contract.get("shot_plan"):
            raise ValueError("Prompt Compiler gate G3 failed: generation beat timeline is incomplete or inconsistent")
        duration_plan = generation_contract.get("duration_plan")
        if not isinstance(duration_plan, dict) or duration_plan.get("mode") != "single_clip":
            raise ValueError("Prompt Compiler gate G3 failed: a single-clip duration plan is required")
        if duration_plan.get("total_duration_seconds") != output["duration_seconds"]:
            raise ValueError("Prompt Compiler gate G3 failed: duration plan and output duration disagree")
        direction = generation_contract.get("production_direction")
        if not isinstance(direction, dict) or not all(isinstance(direction.get(key), str) and direction[key].strip() for key in ("camera_language", "visual_treatment", "motion_continuity")):
            raise ValueError("Prompt Compiler gate G3 failed: production direction is incomplete")
        guards = direction.get("negative_guards")
        if not isinstance(guards, list) or not guards or not all(isinstance(item, str) and item.strip() for item in guards):
            raise ValueError("Prompt Compiler gate G3 failed: production direction guards are incomplete")
        reference = generation_contract.get("reference_strategy")
        if not isinstance(reference, dict) or reference.get("mode") not in {"text_only", "reference_conditioned", "first_frame_conditioned", "reference_retained_not_applied"}:
            raise ValueError("Prompt Compiler gate G4 failed: invalid reference strategy")
        binding = generation_contract.get("provider_binding")
        if not isinstance(binding, dict) or binding.get("intended_mode") not in {"text_to_video", "image_conditioned_video", "image_to_video", "reference_to_video", "unavailable", "selection_pending"}:
            raise ValueError("Prompt Compiler gate G8 failed: provider binding is incomplete")
        sampling = validate_evidence_sampling_policy(
            (evaluation_contract.get("evidence_plan") or {}).get("sampling_policy"),
            field="evaluation_contract.evidence_plan.sampling_policy", allowed_shot_ids=shot_ids,
        )
        windows = sampling.get("shot_windows", [])
        if windows != [{"shot_id": shot["shot_id"], "start_seconds": shot["start_seconds"], "end_seconds": shot["end_seconds"]} for shot in expected_timeline]:
            raise ValueError("Prompt Compiler gate G3 failed: evaluation windows are not bound to the generation timeline")
        if not isinstance(generation_contract.get("repair_delta"), dict):
            raise ValueError("Prompt Compiler gate G6 failed: repair delta is missing")
        criteria_ids = {item.get("requirement_id") for item in evaluation_contract.get("acceptance_criteria", []) if isinstance(item, dict)}
        if not criteria_ids:
            raise ValueError("Prompt Compiler gate G10 failed: evaluation criteria are missing")
        profile = evaluation_contract.get("rubric_profile")
        if not isinstance(profile, dict) or not profile.get("profile_id") or not isinstance(profile.get("diagnostic_weights"), dict):
            raise ValueError("Prompt Compiler gate G10 failed: an executable rubric profile is required")
        audio = evaluation_contract.get("audio_evaluation")
        if not isinstance(audio, dict) or audio.get("mode") not in {"disabled", "presence", "semantic", "sync"}:
            raise ValueError("Prompt Compiler gate G10 failed: audio evaluation mode is invalid")

    @staticmethod
    def _stable_id(prefix: str, payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:12]}"

    @staticmethod
    def _record(state: ProjectState, event: str, payload: Dict[str, Any]) -> None:
        state.history.append({"at": "local-demo", "phase": "compile", "event": event, "payload": payload})

    @staticmethod
    def _sampling_policy(spec, rubric_profile: Dict[str, Any] | None = None) -> Dict[str, Any]:
        source = spec.evidence_sampling or fallback_evidence_sampling_policy(spec.shots, spec.risk_forecast)
        shots = spec.shots
        shot_ids = [shot["shot_id"] for shot in shots]
        action_ids = [shot["shot_id"] for shot in shots if shot.get("beat_function") in {"action", "transition"}]
        first, last = shot_ids[0], shot_ids[-1]
        profile_id = (rubric_profile or {}).get("profile_id")
        if profile_id == "action_short":
            focus = list(dict.fromkeys([first, *(action_ids or shot_ids), last]))[:3]
            source = {
                "focus_shot_ids": focus,
                "critical_moments": [{"shot_id": shot_id, "reason": "Capture the planned action state and its physical plausibility."} for shot_id in focus],
                "rationale": "Motion rubric requires evidence before, during, and after the key action; this prevents a correct-looking end frame from hiding a failed interaction.",
            }
        elif profile_id == "product_ad":
            source = {
                "focus_shot_ids": list(dict.fromkeys([first, last])),
                "critical_moments": [
                    {"shot_id": first, "reason": "Inspect the product's required structure, colour, material, and brand detail."},
                    *([] if last == first else [{"shot_id": last, "reason": "Verify product geometry and required logo/text remain stable in the final display state."}]),
                ],
                "rationale": "Product rubric prioritises first-to-final integrity evidence so aesthetic footage cannot conceal logo or geometry drift.",
            }
        elif profile_id == "character_story":
            source = {
                "focus_shot_ids": list(dict.fromkeys([first, last])),
                "critical_moments": [
                    {"shot_id": first, "reason": "Establish the approved subject identity, attributes, setting, and starting story state."},
                    *([] if last == first else [{"shot_id": last, "reason": "Verify identity and story state remain continuous through the planned outcome."}]),
                ],
                "rationale": "Character-story rubric compares opening and outcome evidence for identity and cross-shot continuity.",
            }
        elif profile_id == "music_video_visual":
            focus = shot_ids[:3]
            source = {
                "focus_shot_ids": focus,
                "critical_moments": [{"shot_id": shot_id, "reason": "Capture the planned performance or expressive beat with stable framing."} for shot_id in focus],
                "rationale": "Visual-only MV rubric samples each planned beat; it makes no claim about audio or lip-sync quality.",
            }
        return validate_evidence_sampling_policy(source, field="control_plan.evidence_sampling", allowed_shot_ids=set(shot_ids))

    @staticmethod
    def _reference_strategy(spec, provider_binding: Dict[str, Any]) -> Dict[str, Any]:
        assets = spec.multimodal_assets or ([spec.reference_asset] if spec.reference_asset else [])
        image_assets = [asset for asset in assets if isinstance(asset, dict) and isinstance(asset.get("preview_data_url"), str) and asset["preview_data_url"].startswith("data:image/")]
        audio_assets = [asset for asset in assets if isinstance(asset, dict) and str(asset.get("media_type", "")).lower() == "audio" and isinstance(asset.get("data_url"), str) and asset["data_url"].startswith("data:audio/")]
        video_assets = [asset for asset in assets if asset.get("media_type") == "video"]
        asset = image_assets[0] if image_assets else (video_assets[0] if video_assets else spec.reference_asset or {})
        if image_assets or audio_assets or video_assets:
            reference_applied = provider_binding.get("intended_mode") in {"image_conditioned_video", "image_to_video", "reference_to_video"}
            first_frame_mode = provider_binding.get("intended_mode") == "image_to_video"
            return {
                "mode": "first_frame_conditioned" if first_frame_mode else "reference_conditioned" if reference_applied else "reference_retained_not_applied",
                "asset_name": str(asset.get("name", "reference image")),
                "asset_count": len(image_assets) + len(audio_assets) + len(video_assets),
                "image_reference_count": len(image_assets),
                "audio_reference_count": len(audio_assets),
                "video_reference_count": len(video_assets),
                "fact_provenance": "user_supplied_asset",
                "honesty_notice": (
                    "Only the first ordered usable image is submitted as the provider's opening-frame condition. Additional images and audio remain in Agent memory and the evaluation contract; it does not guarantee per-frame identity preservation."
                    if first_frame_mode
                    else "The asset conditions the provider request; it does not guarantee per-frame identity preservation."
                    if reference_applied
                    else "The asset remains available to planning and evaluation, but this configured generation adapter is not declared to receive its pixels."
                ),
            }
        return {
            "mode": "text_only",
            "fact_provenance": "no_usable_reference_pixels",
            "honesty_notice": "No visual pixel claim is made from reference metadata alone.",
        }

    def _constraint_trace(self, spec, repair_delta: Dict[str, Any]) -> list[Dict[str, Any]]:
        tool_facts = {
            str(item.get("text", "")).strip()
            for item in spec.enriched_context.get("constraints", [])
            if isinstance(item, dict) and item.get("source") == "tool_fact"
        }
        criteria = spec.acceptance_criteria
        result = []
        for index, constraint in enumerate(spec.immutable_character_attributes, start=1):
            origin = "tool_fact" if constraint in tool_facts else "user_brief" if constraint.startswith("User brief:") else "control_plan"
            related = [item["requirement_id"] for item in criteria if constraint.lower() in (item.get("requirement", "") + " " + item.get("expected", "")).lower()]
            result.append({
                "constraint_trace_id": f"ct_{index}",
                "constraint": constraint,
                "requirement_origin": origin,
                "fact_provenance": "tool_observation" if origin == "tool_fact" else "user_or_plan",
                "generation_fields": ["immutable_constraints", "shot_plan"],
                "evaluation_requirement_ids": related,
                "repair_handling": "preserve" if constraint in repair_delta["preserve_set"] else "unchanged",
            })
        return result

    @staticmethod
    def _criteria_with_trace(
        criteria: Iterable[Dict[str, Any]], trace: list[Dict[str, Any]], rubric_profile: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        output = []
        for criterion in criteria:
            text = (str(criterion.get("requirement", "")) + " " + str(criterion.get("expected", ""))).lower()
            links = [item["constraint_trace_id"] for item in trace if item["constraint"].lower() in text]
            output.append({
                **criterion,
                "constraint_trace_ids": links,
                "rubric_dimension": dimension_for_criterion(criterion, rubric_profile),
            })
        return output

    @staticmethod
    def _quality_policy_criteria(timeline: list[Dict[str, Any]], rubric_profile: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Add bounded, observable craft checks to every evaluated video.

        These are non-critical quality-policy checks rather than invented user
        facts. They give visual-quality and, where relevant, motion/camera
        scores a concrete evidence target even when a concise user brief only
        specified subject and story requirements.
        """
        criteria: list[Dict[str, Any]] = []
        motion_profile = rubric_profile.get("profile_id") in {"action_short", "music_video_visual"}
        for shot in timeline:
            shot_id = str(shot["shot_id"])
            criteria.append({
                "requirement_id": f"quality_visual_integrity_{shot_id.lower()}",
                "shot_id": shot_id,
                "source": "quality_policy",
                "requirement": "The visible frame is free of material generation artifacts that make the planned subject, product, or setting unusable.",
                "expected": "No severe malformed anatomy, duplicate subject/object, broken geometry, unreadable required detail, or distracting artifact is visible at the sampled moment.",
                "critical": False,
                "rubric_dimension": "visual_quality",
                "constraint_trace_ids": [],
            })
            if motion_profile:
                criteria.append({
                    "requirement_id": f"quality_camera_readability_{shot_id.lower()}",
                    "shot_id": shot_id,
                    "source": "quality_policy",
                    "requirement": "The planned subject or action remains visually readable within the selected framing and camera treatment.",
                    "expected": "The sampled moment preserves a legible subject/action with no material framing loss or camera-treatment failure.",
                    "critical": False,
                    "rubric_dimension": "camera_control",
                    "constraint_trace_ids": [],
                })
        return criteria

    @staticmethod
    def _audio_presence_criteria(audio_plan: Dict[str, Any], first_shot_id: str) -> list[Dict[str, Any]]:
        if not audio_plan.get("active"):
            return []
        return [{
            "requirement_id": "audio_presence_integrity",
            "shot_id": first_shot_id,
            "source": "audio_reference_or_prompt",
            "requirement": "The approved audio requirement is present in the generated video.",
            "expected": "A locally inspectable audio track exists for the generated video; further semantic or sync checks run only when their declared tools are available.",
            "critical": True,
            "evidence_mode": "audio_probe",
            "rubric_dimension": "audio_presence_integrity",
            "constraint_trace_ids": [],
        }]

    def _repair_delta(self, state: ProjectState, repair_task: RepairTask | None) -> Dict[str, Any]:
        all_constraints = list(state.creative_spec.immutable_character_attributes)
        if not repair_task:
            return {
                "mode": "initial_generation",
                "preserve_set": all_constraints,
                "change_set": [],
                "evidence_to_address": [],
                "rejected_scope": ["Do not add unverified characters, products, or scene facts."],
            }
        report = state.latest_report
        failed = [
            item for item in (report.atomic_checks if report else [])
            if item.get("shot_id") == repair_task.target_shot and item.get("status") == "fail"
        ]
        change_set = [
            {"requirement_id": item.get("requirement_id"), "target_shot": repair_task.target_shot, "required_change": item.get("repair_hint") or item.get("expected")}
            for item in failed
        ] or [{"target_shot": repair_task.target_shot, "required_change": repair_task.prompt}]
        evidence = [str(item.get("observed", "")).strip() for item in failed if str(item.get("observed", "")).strip()]
        return {
            "mode": "targeted_repair",
            "execution_scope": getattr(repair_task, "execution_scope", "whole_clip_regeneration"),
            "target_shot": repair_task.target_shot,
            "preserve_set": all_constraints,
            "change_set": change_set,
            "evidence_to_address": evidence,
            "rejected_scope": [
                "Do not alter passed acceptance criteria outside the targeted repair.",
                "Do not represent this complete-clip regeneration as a provider-side local shot edit.",
            ],
        }

    def _provider_binding(self, spec, output: Dict[str, Any]) -> Dict[str, Any]:
        route = self.capabilities.route(spec.reference_asset, spec.multimodal_assets)
        choice = spec.video_model_selection
        if self.capabilities.provider == "auto" and choice.get("confirmed"):
            from .model_router import RoutingVideoGenerator
            from ..domain.models import ProjectState
            adapter = RoutingVideoGenerator().adapters.get(choice.get("selected_model"))
            if adapter is None:
                raise ValueError("Selected video adapter is unavailable")
            route = adapter.select_tool(ProjectState(project_id="binding", creative_spec=spec))
        return {
            "adapter_id": route["provider_id"],
            "adapter_version": "2",
            "model_id": route["model_id"],
            "tool_name": route["tool_name"],
            "capability_profile": route["capability_profile"],
            "intended_mode": route["request_mode"],
            "selection_reason": route["reason"],
            "rejected_option": route["rejected_option"],
            "output_spec": output,
        }
