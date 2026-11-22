"""Ark Seedance video-generation tool used by the Agent's Act step."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict

from ..domain.models import GenerationJob, ProjectState, RepairTask
from ..agent.generation_spec import fallback_generation_spec, validate_generation_spec
from ..agent.video_capabilities import VideoCapabilityRegistry


class ArkVideoGenerator:
    """Submit and observe asynchronous Seedance video-generation jobs."""

    endpoint = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"

    def __init__(self, api_key: str | None = None, text_model: str | None = None, reference_model: str | None = None):
        self.api_key = api_key or os.getenv("ARK_API_KEY")
        self.text_model = text_model or os.getenv("ARK_VIDEO_TEXT_MODEL")
        self.reference_model = reference_model or os.getenv("ARK_VIDEO_REFERENCE_MODEL")
        self.capabilities = VideoCapabilityRegistry(self.text_model, self.reference_model)

    def available(self) -> bool:
        return bool(self.api_key and self.text_model)

    def select_tool(self, state: ProjectState) -> Dict[str, Any]:
        return self.capabilities.route(state.creative_spec.reference_asset)

    def submit(self, state: ProjectState, version_id: str, repair_task: RepairTask | None = None) -> GenerationJob:
        if not self.available():
            raise ValueError("Video generation is not configured. Add ARK_VIDEO_TEXT_MODEL to .env and ensure the Ark API key has Seedance access.")
        selection = self.select_tool(state)
        if selection["request_mode"] == "unavailable":
            raise ValueError(selection["reason"])
        prompt = self._generation_prompt(state, repair_task)
        content: list[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if selection["request_mode"] == "image_conditioned_video":
            content.append({
                "type": "image_url",
                "image_url": {"url": state.creative_spec.reference_asset["preview_data_url"]},
            })
        body = json.dumps({
            "model": selection["model_id"],
            "content": content,
            "return_last_frame": True,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:420]
            raise ValueError(f"Seedance rejected the generation request (HTTP {exc.code}): {detail}") from exc
        except OSError as exc:
            raise ValueError("Seedance could not be reached. Check network access and try again.") from exc
        job_id = parsed.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("Seedance did not return a generation task ID")
        return GenerationJob(
            job_id=job_id,
            version_id=version_id,
            tool_name=selection["tool_name"],
            model_id=selection["model_id"],
            prompt=prompt,
            provider_id=selection["provider_id"],
            selection_reason=selection["reason"],
            rejected_option=selection["rejected_option"],
            request_mode=selection["request_mode"],
            capability_profile_id=selection["capability_profile_id"],
            capability_notice=selection["capability_profile"].get("verification_notice", ""),
            compile_run_id=str(state.creative_spec.compile_run_id),
            generation_contract_id=str((state.creative_spec.generation_contract or {}).get("generation_contract_id", "")),
            evaluation_contract_id=str((state.creative_spec.evaluation_contract or {}).get("evaluation_contract_id", "")),
        )

    def refresh(self, job: GenerationJob) -> GenerationJob:
        request = urllib.request.Request(
            f"{self.endpoint}/{job.job_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:360]
            raise ValueError(f"Seedance task lookup failed (HTTP {exc.code}): {detail}") from exc
        except OSError as exc:
            raise ValueError("Seedance task status could not be retrieved") from exc
        status = str(parsed.get("status", "")).lower()
        if status not in {"queued", "running", "succeeded", "failed", "canceled", "cancelled", "expired"}:
            raise ValueError("Seedance returned an unrecognized task status; retain the existing task and retry polling")
        job.status = status
        content = parsed.get("content") if isinstance(parsed.get("content"), dict) else {}
        video_url = content.get("video_url") or parsed.get("video_url")
        if isinstance(video_url, str) and video_url.startswith("https://"):
            job.output_video_url = video_url
        error = parsed.get("error") or parsed.get("message")
        if job.status in {"failed", "canceled", "cancelled", "expired"}:
            job.error = str(error)[:500] if error else f"Seedance task {job.status}; no replacement task was submitted"
        return job

    @staticmethod
    def _generation_prompt(
        state: ProjectState,
        repair_task: RepairTask | None,
        include_output_hints: bool = True,
    ) -> str:
        spec = state.creative_spec
        contract = spec.generation_contract or {}
        if contract.get("generation_contract_id"):
            output = validate_generation_spec(contract.get("output_spec"))
            shot_text = " ".join(
                f"{shot.get('shot_id')} ({shot.get('start_seconds', '?')}–{shot.get('end_seconds', '?')}s; verify: {shot.get('critical_moment', 'planned visual beat')}): {shot.get('intent')}"
                for shot in contract.get("shot_plan", [])
                if isinstance(shot, dict)
            )
            constraints = "; ".join(str(item) for item in contract.get("immutable_constraints", []))
            direction = contract.get("production_direction") or {}
            direction_text = " ".join([
                f"Agent camera direction: {direction.get('camera_language', '')}",
                f"Agent visual treatment: {direction.get('visual_treatment', '')}",
                f"Agent continuity direction: {direction.get('motion_continuity', '')}",
                f"Agent sound direction: {direction.get('sound_design', '')}",
                f"Agent negative guards: {'; '.join(str(item) for item in direction.get('negative_guards', []))}",
            ]).strip()
            delta = contract.get("repair_delta") or {}
            repair_items = "; ".join(
                str(item.get("required_change", ""))
                for item in delta.get("change_set", []) if isinstance(item, dict)
            )
            repair = f" Targeted repair only: {repair_items}." if repair_items else ""
            quality_instruction = "Prioritise crisp subject edges, readable important details, and stable visual texture." if output["quality_target"] == "high" else "Prioritise a coherent, efficient first-pass visual result."
            result = (
                f"Create one coherent short video following this objective: {contract.get('objective', spec.goal)}. "
                f"Follow this approved semantic-beat timeline in order: {shot_text}. The beat boundaries are control targets, not a demand for hard cuts. Preserve these immutable constraints: {constraints}."
                f" {direction_text} Output intent: {output['rationale']} {quality_instruction}{repair}"
            )
            return f"{result} --ratio {output['aspect_ratio']} --dur {output['duration_seconds']}" if include_output_hints else result
        output = validate_generation_spec(spec.generation_spec) if spec.generation_spec else fallback_generation_spec(spec.source_prompt, spec.reference_asset)
        shot_text = " ".join(
            f"{shot['shot_id']} ({shot.get('start_seconds', '?')}–{shot.get('end_seconds', '?')}s): {shot['intent']}"
            for shot in spec.shots
        )
        constraints = "; ".join(spec.immutable_character_attributes)
        repair = f" Repair priority: {repair_task.prompt}" if repair_task else ""
        quality_instruction = "Prioritise crisp subject edges, readable important details, and stable visual texture." if output["quality_target"] == "high" else "Prioritise a coherent, efficient first-pass visual result."
        result = (
            f"Create one coherent short video following this objective: {spec.goal}. "
            f"Use this ordered semantic-beat timeline: {shot_text}. "
            f"Preserve these immutable constraints: {constraints}."
            f" Output intent: {output['rationale']} {quality_instruction}{repair}"
        )
        return f"{result} --ratio {output['aspect_ratio']} --dur {output['duration_seconds']}" if include_output_hints else result
