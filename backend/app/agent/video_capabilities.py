"""Versioned, local capability declarations for video-generation adapters.

These declarations are deliberately *not* a claim about a provider's latest
public API.  They state only what this local adapter has been configured and
verified to submit.  Keeping that distinction in code lets the Agent choose a
valid tool without baking a changing model catalogue into the creative or
quality-control loop.
"""

from __future__ import annotations

import os
from typing import Any


class VideoCapabilityRegistry:
    """Route a generation request through an explicitly declared adapter mode."""

    def __init__(
        self,
        text_model: str | None = None,
        reference_model: str | None = None,
        provider: str | None = None,
        minimax_api_key: str | None = None,
        minimax_model: str | None = None,
    ) -> None:
        self.text_model = (text_model if text_model is not None else os.getenv("ARK_VIDEO_TEXT_MODEL", "")).strip()
        self.reference_model = (reference_model if reference_model is not None else os.getenv("ARK_VIDEO_REFERENCE_MODEL", "")).strip()
        self.provider = (provider if provider is not None else os.getenv("VIDEO_PROVIDER", "ark")).strip().lower()
        self.minimax_api_key = (minimax_api_key if minimax_api_key is not None else os.getenv("MINIMAX_API_KEY", "")).strip()
        default_minimax_model = "MiniMax-H3" if self.provider in {"minimax_h3", "h3"} else "MiniMax-Hailuo-2.3"
        self.minimax_model = (minimax_model if minimax_model is not None else os.getenv("MINIMAX_VIDEO_MODEL", default_minimax_model)).strip()

    @staticmethod
    def _has_reference_pixels(reference_asset: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(reference_asset, dict)
            and isinstance(reference_asset.get("preview_data_url"), str)
            and reference_asset["preview_data_url"].startswith("data:image/")
        )

    @staticmethod
    def _is_seedance_1_pro(model_id: str) -> bool:
        return "seedance-1-0-pro" in model_id.lower().replace("_", "-")

    def route(
        self,
        reference_asset: dict[str, Any] | None = None,
        multimodal_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return the selected tool and a compact, auditable capability snapshot."""
        assets = multimodal_assets or ([reference_asset] if reference_asset else [])
        has_reference = any(self._has_reference_pixels(asset) for asset in assets)
        has_audio_reference = any(
            isinstance(asset, dict)
            and str(asset.get("media_type", "")).lower() == "audio"
            and isinstance(asset.get("data_url"), str)
            and asset["data_url"].startswith("data:audio/")
            for asset in assets
        )
        if self.provider == "auto":
            return {"route":"selection_pending", "request_mode":"selection_pending", "tool_name":"select_video_model",
                    "model_id":"", "provider_id":"model_router", "capability_profile_id":"pending_model_selection",
                    "capability_profile":{"profile_id":"pending_model_selection", "lifecycle":"pending_user_selection", "verification_notice":"模型推荐需经用户确认后才会执行生成。"},
                    "reason":"先根据需求筛选候选，再由用户确认模型。", "rejected_option":"不自动提交未经确认的模型。", "capability_snapshot":"selection_pending"}
        if self.provider in {"minimax_h3", "h3"}:
            return self._minimax_h3_route(has_reference, has_audio_reference)
        if self.provider in {"minimax", "minimax_hailuo", "hailuo"}:
            first_image = next((asset for asset in assets if self._has_reference_pixels(asset)), None)
            return self._minimax_hailuo_route(first_image, has_audio_reference, len(assets))
        if not self.text_model:
            return {
                "route": "unavailable",
                "request_mode": "unavailable",
                "tool_name": "generation_unavailable",
                "model_id": "",
                "provider_id": "none",
                "capability_profile_id": "no_configured_video_adapter",
                "capability_profile": self._unavailable_profile(),
                "reason": "No enabled video-generation adapter is configured for this local run.",
                "rejected_option": "No provider request is submitted until a configured adapter is available.",
                "rejected": ["text_to_video: no configured provider", "image_conditioned_video: no configured provider"],
                "capability_snapshot": "configured=false; routing=blocked",
            }

        # A separate reference endpoint is an explicit operator declaration.
        # It is the only condition under which this demo sends image pixels to
        # Ark; a plain text endpoint must never be guessed to accept them.
        if has_reference and self.reference_model:
            profile = self._explicit_reference_profile(self.reference_model)
            return {
                "route": "image_to_video",
                "request_mode": "image_conditioned_video",
                "tool_name": "generate_reference_video",
                "model_id": self.reference_model,
                "provider_id": "ark_video_adapter",
                "capability_profile_id": profile["profile_id"],
                "capability_profile": profile,
                "reason": "A user reference image is available and a separately declared reference-capable adapter is configured.",
                "rejected_option": "generate_text_video: it would omit user-supplied reference pixels despite an available reference adapter.",
                "rejected": ["generate_text_video: weaker for preserving the supplied visual reference"],
                "capability_snapshot": "text_to_video=true; image_conditioned_video=explicitly_configured; unverified_modes=disabled",
            }

        profile = self._text_profile(self.text_model)
        if has_reference:
            legacy_notice = (
                "The configured Seedance 1.0 Pro adapter is retained for testing and is not declared reference-conditioned in this demo."
                if profile["lifecycle"] == "test_only"
                else "No separately declared reference-capable adapter is configured, so reference pixels are retained in project memory but not sent to the text-to-video request."
            )
            return {
                "route": "text_to_video",
                "request_mode": "text_to_video",
                "tool_name": "generate_text_video",
                "model_id": self.text_model,
                "provider_id": "ark_video_adapter",
                "capability_profile_id": profile["profile_id"],
                "capability_profile": profile,
                "reason": f"{legacy_notice} The least-assumption valid route is text-to-video.",
                "rejected_option": "generate_reference_video: no separately configured adapter has declared support for reference pixels.",
                "rejected": ["generate_reference_video: reference support has not been explicitly configured for this adapter"],
                "capability_snapshot": "text_to_video=true; image_conditioned_video=false; reference_asset=retained_not_applied",
            }

        return {
            "route": "text_to_video",
            "request_mode": "text_to_video",
            "tool_name": "generate_text_video",
            "model_id": self.text_model,
            "provider_id": "ark_video_adapter",
            "capability_profile_id": profile["profile_id"],
            "capability_profile": profile,
            "reason": "No usable reference image is attached, so text-to-video is the lowest-complexity configured route.",
            "rejected_option": "generate_reference_video: no user reference pixels are available for a reference-conditioned request.",
            "rejected": ["generate_reference_video: no usable reference asset"],
            "capability_snapshot": "text_to_video=true; image_conditioned_video=not_requested",
        }

    def _minimax_h3_route(self, has_reference: bool, has_audio_reference: bool) -> dict[str, Any]:
        """Route to the official H3 V2 multimodal ``content`` request."""
        if not self.minimax_api_key:
            return {
                "route": "unavailable",
                "request_mode": "unavailable",
                "tool_name": "generation_unavailable",
                "model_id": self.minimax_model,
                "provider_id": "minimax_h3_adapter",
                "capability_profile_id": "minimax_h3_key_missing",
                "capability_profile": self._unavailable_profile(),
                "reason": "MiniMax H3 is selected but MINIMAX_API_KEY is not configured for this local run.",
                "rejected_option": "No provider request is submitted until a MiniMax API key is available.",
                "rejected": ["reference_to_video: MiniMax API key missing"],
                "capability_snapshot": "provider=minimax_h3; configured=false; routing=blocked",
            }
        profile = self._minimax_h3_profile()
        if has_reference or has_audio_reference:
            modalities = []
            if has_reference:
                modalities.append("reference_image")
            if has_audio_reference:
                modalities.append("reference_audio")
            return {
                "route": "reference_to_video",
                "request_mode": "reference_to_video",
                "tool_name": "generate_multimodal_reference_video",
                "model_id": self.minimax_model,
                "provider_id": "minimax_h3_adapter",
                "capability_profile_id": profile["profile_id"],
                "capability_profile": profile,
                "reason": "MiniMax H3 V2 accepts the attached user reference modalities through its documented content array: " + ", ".join(modalities) + ".",
                "rejected_option": "generate_text_video: it would discard available multimodal reference evidence.",
                "rejected": ["generate_text_video: weaker than the documented multimodal reference route"],
                "capability_snapshot": "text_to_video=true; reference_to_video=images_and_audio; first_last_frame=not_used",
            }
        return {
            "route": "text_to_video",
            "request_mode": "text_to_video",
            "tool_name": "generate_text_video",
            "model_id": self.minimax_model,
            "provider_id": "minimax_h3_adapter",
            "capability_profile_id": profile["profile_id"],
            "capability_profile": profile,
            "reason": "No usable multimodal reference is attached, so MiniMax H3 text-to-video is the lowest-complexity valid route.",
            "rejected_option": "generate_multimodal_reference_video: no usable reference asset is available.",
            "rejected": ["generate_multimodal_reference_video: no usable reference asset"],
            "capability_snapshot": "text_to_video=true; reference_to_video=not_requested",
        }

    def _minimax_hailuo_route(
        self,
        first_image: dict[str, Any] | None,
        has_audio_reference: bool,
        asset_count: int,
    ) -> dict[str, Any]:
        """Choose a documented Hailuo route from the ordered asset memory.

        The public Hailuo I2V API accepts one explicit ``first_frame_image``.
        Attaching images authorises the first uploaded usable image to become
        the opening-frame condition. Remaining images and every audio asset
        stay in project memory, the prompt compiler, and evaluation contract.
        """
        if not self.minimax_api_key:
            return {
                "route": "unavailable",
                "request_mode": "unavailable",
                "tool_name": "generation_unavailable",
                "model_id": self.minimax_model,
                "provider_id": "minimax_hailuo_adapter",
                "capability_profile_id": "minimax_hailuo_key_missing",
                "capability_profile": self._unavailable_profile(),
                "reason": "MiniMax Hailuo is selected but MINIMAX_API_KEY is not configured for this local run.",
                "rejected_option": "No provider request is submitted until a MiniMax API key is available.",
                "rejected": ["text_to_video: MiniMax API key missing"],
                "capability_snapshot": "provider=minimax_hailuo; configured=false; routing=blocked",
            }
        profile = self._minimax_hailuo_profile()
        if first_image:
            return {
                "route": "image_to_video",
                "request_mode": "image_to_video",
                "tool_name": "generate_first_frame_video",
                "model_id": self.minimax_model,
                "provider_id": "minimax_hailuo_adapter",
                "capability_profile_id": profile["profile_id"],
                "capability_profile": profile,
                "applied_reference_name": str(first_image.get("name", "first uploaded image")),
                "reason": (
                    f"The first usable image in the ordered reference set ({first_image.get('name', 'reference image')}) "
                    "is submitted through Hailuo's documented first_frame_image route. "
                    f"The remaining {max(asset_count - 1, 0)} reference asset(s) remain ordered project memory; "
                    "audio is retained for planning and quality evaluation, not sent as an undocumented generation input."
                ),
                "rejected_option": "generate_multimodal_reference_video: the verified Hailuo adapter accepts one first-frame image, not a general multi-image or audio conditioning payload.",
                "rejected": ["generate_multimodal_reference_video: only one documented first-frame image is supported", "generate_audio_conditioned_video: no verified audio-conditioned request path"],
                "capability_snapshot": "text_to_video=true; image_to_video=first_frame_image; additional_images_audio=retained_in_memory_not_submitted",
            }
        if has_audio_reference:
            retained_notice = "Audio reference material is retained in project memory and quality planning, but this verified Hailuo API has no audio-conditioned generation field."
        else:
            retained_notice = "No usable reference image is attached, so Hailuo text-to-video is the lowest-complexity configured route."
        return {
            "route": "text_to_video",
            "request_mode": "text_to_video",
            "tool_name": "generate_text_video",
            "model_id": self.minimax_model,
            "provider_id": "minimax_hailuo_adapter",
            "capability_profile_id": profile["profile_id"],
            "capability_profile": profile,
            "reason": retained_notice,
            "rejected_option": "generate_first_frame_video: no user reference image is available.",
            "rejected": ["generate_first_frame_video: no usable reference asset"],
            "capability_snapshot": "text_to_video=true; first_frame=not_requested; audio=retained_not_submitted" if has_audio_reference else "text_to_video=true; first_frame=not_requested",
        }

    @staticmethod
    def _unavailable_profile() -> dict[str, Any]:
        return {
            "profile_id": "no_configured_video_adapter",
            "profile_source": "local_adapter_declaration",
            "lifecycle": "unconfigured",
            "supported_modes": [],
            "reference_mode": "not_available",
            "verification_notice": "No provider capability is assumed from an unset model ID.",
        }

    def _text_profile(self, model_id: str) -> dict[str, Any]:
        legacy = self._is_seedance_1_pro(model_id)
        return {
            "profile_id": "seedance_1_0_pro_test" if legacy else "ark_text_video_declared",
            "profile_source": "local_adapter_declaration",
            "model_id": model_id,
            "lifecycle": "test_only" if legacy else "configured",
            "supported_modes": ["text_to_video"],
            "reference_mode": "not_declared",
            "verification_notice": (
                "This legacy Seedance 1.0 Pro adapter is used only for local loop testing. It is not presented as a current production recommendation."
                if legacy
                else "The configured model is declared text-to-video only unless a separate reference endpoint is configured."
            ),
            "output_support": {"duration_policy": "Agent compiled", "aspect_ratio_policy": "Agent compiled", "quality_policy": "Agent compiled"},
        }

    @staticmethod
    def _explicit_reference_profile(model_id: str) -> dict[str, Any]:
        return {
            "profile_id": "ark_reference_video_declared",
            "profile_source": "operator_configured_adapter",
            "model_id": model_id,
            "lifecycle": "configured",
            "supported_modes": ["image_conditioned_video"],
            "reference_mode": "single_image",
            "verification_notice": "Reference-image submission is enabled only because this endpoint was explicitly configured for it; multi-reference and video editing remain disabled.",
            "output_support": {"duration_policy": "Agent compiled", "aspect_ratio_policy": "Agent compiled", "quality_policy": "Agent compiled"},
        }

    def _minimax_hailuo_profile(self) -> dict[str, Any]:
        return {
            "profile_id": "minimax_hailuo_2_3_official",
            "profile_source": "official_hailuo_adapter",
            "model_id": self.minimax_model,
            "lifecycle": "configured",
            "supported_modes": ["text_to_video", "image_to_video"],
            "reference_mode": "first_uploaded_usable_image_as_first_frame",
            "verification_notice": "This verified MiniMax Hailuo adapter submits prompt, duration, resolution, and—when an image is attached—only the first ordered usable image as first_frame_image. Additional images and all audio remain ordered project memory and appear in the audit trail; they are not submitted through undocumented provider fields.",
            "output_support": {
                "duration_policy": "Hailuo 2.3 accepts 6s or 10s at 768P and 6s at 1080P in this adapter. The control plan must match a supported duration before the Agent acts.",
                "aspect_ratio_policy": "Provider default framing; this endpoint adapter does not claim a custom aspect-ratio parameter.",
                "quality_policy": "resolution configured by operator",
            },
        }

    def _minimax_h3_profile(self) -> dict[str, Any]:
        return {
            "profile_id": "minimax_h3_v2_official",
            "profile_source": "official_minimax_h3_v2_adapter",
            "model_id": self.minimax_model,
            "lifecycle": "configured",
            "supported_modes": ["text_to_video", "reference_to_video"],
            "reference_mode": "multimodal_reference_images_and_audio",
            "verification_notice": "This MiniMax H3 V2 adapter submits an ordered content array: text plus up to nine reference images and up to three reference audio files. It does not mix first/last-frame inputs with multimodal-reference inputs.",
            "output_support": {
                "duration_policy": "MiniMax H3 accepts an Agent-compiled duration from 4 to 15 seconds.",
                "aspect_ratio_policy": "Agent-compiled ratio, subject to the provider's documented mode rules.",
                "quality_policy": "768P or 2K, configured by operator.",
            },
        }
