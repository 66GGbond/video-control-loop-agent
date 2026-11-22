"""Official MiniMax H3 V2 video-generation adapter for the Agent Act step.

The adapter intentionally mirrors the documented V2 shape: a required text
item plus ordered multimodal reference items in ``content``.  It never turns a
general reference image into a first frame, because first/last-frame inputs and
multimodal-reference inputs are mutually exclusive H3 modes.
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


class MiniMaxH3Generator:
    """Submit and observe official asynchronous MiniMax H3 V2 tasks."""

    default_base_url = "https://api.minimaxi.com"
    supported_models = {"MiniMax-H3"}
    supported_resolutions = {"768P", "2K"}

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        resolution: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("MINIMAX_API_KEY")
        self.model = model or os.getenv("MINIMAX_VIDEO_MODEL", "MiniMax-H3")
        self.resolution = (resolution or os.getenv("MINIMAX_VIDEO_RESOLUTION", "768P")).upper()
        self.base_url = (base_url or os.getenv("MINIMAX_API_BASE_URL", self.default_base_url)).rstrip("/")
        self.capabilities = VideoCapabilityRegistry(
            provider="minimax_h3",
            minimax_api_key=self.api_key,
            minimax_model=self.model,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v2/video_generation"

    def available(self) -> bool:
        return bool(self.api_key and self.model in self.supported_models and self.resolution in self.supported_resolutions)

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
                raise ValueError(f"MiniMax H3 requires an available API balance (HTTP 402): {detail}") from exc
            raise ValueError(f"MiniMax H3 rejected the generation request (HTTP {exc.code}): {detail}") from exc
        except OSError as exc:
            reason = str(getattr(exc, "reason", "network request failed"))[:180]
            raise ValueError(f"MiniMax H3 could not be reached ({reason}). Check network or proxy settings and try again.") from exc
        job_id = parsed.get("task_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("MiniMax H3 did not return a generation task ID")
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
        # H3 Video Generation V2 uses a path parameter here, unlike the
        # earlier MiniMax video APIs that accepted ``?task_id=...``. Sending
        # the older shape can yield a response that never exposes the actual
        # task terminal state, leaving the UI in an endless generating loop.
        task_id = urllib.parse.quote(job.job_id, safe="")
        request = urllib.request.Request(
            f"{self.base_url}/v2/query/video_generation/{task_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:360]
            raise ValueError(f"MiniMax H3 task lookup failed (HTTP {exc.code}): {detail}") from exc
        except OSError as exc:
            reason = str(getattr(exc, "reason", "network request failed"))[:180]
            raise ValueError(f"MiniMax H3 task status could not be retrieved ({reason})") from exc

        task = parsed.get("task") if isinstance(parsed.get("task"), dict) else parsed
        raw_status = str(task.get("status", "")).strip().lower()
        status_map = {
            "queued": "queued",
            "preparing": "queued",
            "queueing": "queued",
            "running": "running",
            "processing": "running",
            "succeeded": "succeeded",
            "success": "succeeded",
            "failed": "failed",
            "fail": "failed",
            "cancelled": "failed",
            "canceled": "failed",
        }
        # Do not silently treat a malformed/unknown provider response as a
        # still-running job. The user needs a truthful error and a retry path.
        job.status = status_map.get(raw_status, "failed")
        if job.status == "succeeded":
            content = task.get("content") if isinstance(task.get("content"), dict) else {}
            url = content.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                raise ValueError("MiniMax H3 completed the task without a downloadable video URL")
            job.output_video_url = url
        error = parsed.get("base_resp") or parsed.get("error") or parsed.get("message") or task.get("error")
        if job.status == "failed":
            if error:
                job.error = self._error_text(error)
            elif raw_status in {"cancelled", "canceled"}:
                job.error = "MiniMax H3 cancelled the generation task."
            else:
                status_label = raw_status or "missing"
                job.error = f"MiniMax H3 returned an unsupported task status: {status_label}."
        return job

    def _request_body(self, state: ProjectState, selection: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        contract = state.creative_spec.generation_contract or {}
        output = validate_generation_spec(contract.get("output_spec") or state.creative_spec.generation_spec)
        if output["duration_seconds"] > 15:
            raise ValueError("MiniMax H3 supports at most 15 seconds per clip")
        assets = state.creative_spec.multimodal_assets or []
        if sum(a.get("media_type") == "image" for a in assets) > 9 or sum(a.get("media_type") == "audio" for a in assets) > 3 or any(a.get("media_type") == "video" for a in assets):
            raise ValueError("References exceed the implemented H3 adapter limits")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if selection["request_mode"] == "reference_to_video":
            assets = state.creative_spec.multimodal_assets or (
                [state.creative_spec.reference_asset] if state.creative_spec.reference_asset else []
            )
            image_count = 0
            audio_count = 0
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                image = asset.get("preview_data_url")
                audio = asset.get("data_url")
                if isinstance(image, str) and image.startswith("data:image/") and image_count < 9:
                    content.append({"type": "image_url", "image_url": {"url": image}, "role": "reference_image"})
                    image_count += 1
                elif isinstance(audio, str) and audio.startswith("data:audio/") and audio_count < 3:
                    # Browsers label an MP3 file as ``audio/mpeg``. H3's
                    # V2 parser maps that media subtype to a `.mpeg` file,
                    # while its documented accepted audio format is `.mp3`.
                    # Keep the user's bytes unchanged and normalise only the
                    # data-URL declaration at the provider boundary.
                    content.append({
                        "type": "audio_url",
                        "audio_url": {"url": self._normalise_audio_data_url(audio)},
                        "role": "reference_audio",
                    })
                    audio_count += 1
            if len(content) == 1:
                raise ValueError("MiniMax H3 multimodal reference route was selected but no usable reference data is available")
        return {
            "model": selection["model_id"],
            "content": content,
            "resolution": self.resolution,
            "duration": output["duration_seconds"],
            "ratio": output["aspect_ratio"],
        }

    def _configuration_error(self) -> str:
        if not self.api_key:
            return "MiniMax H3 is not configured. Add MINIMAX_API_KEY to .env."
        if self.model not in self.supported_models:
            return f"MINIMAX_VIDEO_MODEL={self.model!r} is not supported by this H3 adapter. Set it to MiniMax-H3."
        return "MINIMAX_VIDEO_RESOLUTION must be 768P or 2K."

    @staticmethod
    def _normalise_audio_data_url(audio_url: str) -> str:
        """Translate browser MIME aliases to the H3-supported file labels."""
        replacements = {
            "data:audio/mpeg;": "data:audio/mp3;",
            "data:audio/x-mpeg;": "data:audio/mp3;",
            "data:audio/x-wav;": "data:audio/wav;",
            "data:audio/wave;": "data:audio/wav;",
        }
        for source, target in replacements.items():
            if audio_url.startswith(source):
                return f"{target}{audio_url[len(source):]}"
        return audio_url

    @staticmethod
    def _error_text(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("status_msg") or value.get("message") or value.get("type") or "MiniMax H3 task failed")[:500]
        return str(value)[:500]
