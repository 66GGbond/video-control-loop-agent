"""Official MiniMax Hailuo video-generation adapter.

This adapter implements the documented Hailuo text-to-video and image-to-video
paths. When the user attaches images, the first ordered usable image becomes
the documented ``first_frame_image`` input. Every additional image and all
audio references remain in Agent memory and the audit trail; they are never
silently encoded into an undocumented provider payload.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from ..agent.generation_spec import validate_generation_spec
from ..agent.video_capabilities import VideoCapabilityRegistry
from ..domain.models import GenerationJob, ProjectState, RepairTask
from .generator import ArkVideoGenerator


class MiniMaxHailuoGenerator:
    """Submit and observe documented asynchronous MiniMax Hailuo tasks."""

    default_base_url = "https://api.minimax.io"
    supported_models = {"MiniMax-Hailuo-2.3"}
    supported_resolutions = {"768P", "1080P"}
    supported_durations = {
        "768P": {6, 10},
        "1080P": {6},
    }

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        resolution: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("MINIMAX_API_KEY")
        self.model = model or os.getenv("MINIMAX_VIDEO_MODEL", "MiniMax-Hailuo-2.3")
        self.resolution = (resolution or os.getenv("MINIMAX_VIDEO_RESOLUTION", "768P")).upper()
        self.base_url = (base_url or os.getenv("MINIMAX_API_BASE_URL", self.default_base_url)).rstrip("/")
        self.capabilities = VideoCapabilityRegistry(
            provider="minimax_hailuo",
            minimax_api_key=self.api_key,
            minimax_model=self.model,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/video_generation"

    def available(self) -> bool:
        return bool(
            self.api_key
            and self.model in self.supported_models
            and self.resolution in self.supported_resolutions
        )

    def select_tool(self, state: ProjectState) -> Dict[str, Any]:
        return self.capabilities.route(state.creative_spec.reference_asset, state.creative_spec.multimodal_assets)

    def submit(self, state: ProjectState, version_id: str, repair_task: RepairTask | None = None) -> GenerationJob:
        if not self.available():
            raise ValueError(self._configuration_error())
        selection = self.select_tool(state)
        if selection["request_mode"] == "unavailable":
            raise ValueError(selection["reason"])
        prompt = ArkVideoGenerator._generation_prompt(state, repair_task, include_output_hints=False)
        body = self._request_body(state, selection, prompt)
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:420]
            if exc.code == 402:
                raise ValueError(f"MiniMax Hailuo requires an available API balance (HTTP 402): {detail}") from exc
            raise ValueError(f"MiniMax Hailuo rejected the generation request (HTTP {exc.code}): {detail}") from exc
        except OSError as exc:
            reason = str(getattr(exc, "reason", "network request failed"))[:180]
            raise ValueError(f"MiniMax Hailuo could not be reached ({reason}). Check network or proxy settings and try again.") from exc
        job_id = parsed.get("task_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("MiniMax Hailuo did not return a generation task ID")
        notice = selection["capability_profile"].get("verification_notice", "")
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
            capability_notice=notice,
            compile_run_id=str(state.creative_spec.compile_run_id),
            generation_contract_id=str((state.creative_spec.generation_contract or {}).get("generation_contract_id", "")),
            evaluation_contract_id=str((state.creative_spec.evaluation_contract or {}).get("evaluation_contract_id", "")),
        )

    def refresh(self, job: GenerationJob) -> GenerationJob:
        query = urllib.parse.urlencode({"task_id": job.job_id})
        request = urllib.request.Request(
            f"{self.base_url}/v1/query/video_generation?{query}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:360]
            raise ValueError(f"MiniMax Hailuo task lookup failed (HTTP {exc.code}): {detail}") from exc
        except OSError as exc:
            reason = str(getattr(exc, "reason", "network request failed"))[:180]
            raise ValueError(f"MiniMax Hailuo task status could not be retrieved ({reason})") from exc

        raw_status = str(parsed.get("status", "Processing")).lower()
        status_map = {
            "preparing": "queued", "queueing": "queued", "processing": "running",
            "success": "succeeded", "failed": "failed", "fail": "failed",
        }
        job.status = status_map.get(raw_status, "running")
        if job.status == "succeeded":
            file_id = parsed.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise ValueError("MiniMax Hailuo completed the task without a downloadable file ID")
            job.output_video_url = self._download_url(file_id)
        error = parsed.get("base_resp") or parsed.get("error") or parsed.get("message")
        if job.status == "failed" and error:
            job.error = self._error_text(error)
        return job

    def _download_url(self, file_id: str) -> str:
        query = urllib.parse.urlencode({"file_id": file_id})
        request = urllib.request.Request(
            f"{self.base_url}/v1/files/retrieve?{query}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:360]
            raise ValueError(f"MiniMax Hailuo file retrieval failed (HTTP {exc.code}): {detail}") from exc
        file_info = parsed.get("file") if isinstance(parsed.get("file"), dict) else parsed
        url = file_info.get("download_url") if isinstance(file_info, dict) else None
        if not isinstance(url, str) or not url.startswith("http"):
            raise ValueError("MiniMax Hailuo did not return a downloadable video URL")
        return url

    def _request_body(self, state: ProjectState, selection: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        contract = state.creative_spec.generation_contract or {}
        output = validate_generation_spec(contract.get("output_spec") or state.creative_spec.generation_spec)
        duration = output["duration_seconds"]
        allowed = self.supported_durations[self.resolution]
        if duration not in allowed:
            allowed_text = " or ".join(f"{item}s" for item in sorted(allowed))
            raise ValueError(
                f"MiniMax Hailuo 2.3 at {self.resolution} supports one {allowed_text} clip. "
                f"This approved plan requests {duration}s. Update the control plan before generating."
            )
        body = {
            "model": selection["model_id"],
            "prompt": prompt,
            "duration": duration,
            "resolution": self.resolution,
        }
        if selection["request_mode"] == "image_to_video":
            image = self._first_usable_image(state)
            if not image:
                raise ValueError("Hailuo image-to-video was selected but no usable first-frame image is available")
            body["first_frame_image"] = image
        return body

    @staticmethod
    def _first_usable_image(state: ProjectState) -> str | None:
        assets = state.creative_spec.multimodal_assets or (
            [state.creative_spec.reference_asset] if state.creative_spec.reference_asset else []
        )
        for asset in assets:
            image = asset.get("preview_data_url") if isinstance(asset, dict) else None
            if isinstance(image, str) and image.startswith("data:image/"):
                return image
        return None

    def _configuration_error(self) -> str:
        if not self.api_key:
            return "MiniMax Hailuo is not configured. Add MINIMAX_API_KEY to .env."
        if self.model not in self.supported_models:
            return (
                f"MINIMAX_VIDEO_MODEL={self.model!r} is not supported by this official Hailuo adapter. "
                "Set it to MiniMax-Hailuo-2.3."
            )
        return "MINIMAX_VIDEO_RESOLUTION must be 768P or 1080P."

    @staticmethod
    def _error_text(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("status_msg") or value.get("message") or value.get("type") or "MiniMax Hailuo task failed")[:500]
        return str(value)[:500]
