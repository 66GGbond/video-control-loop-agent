"""Output-spec policy for the video-generation tool.

The planner owns the creative decision; this module only validates it and
provides a transparent fallback when the live planner is unavailable.
"""

from __future__ import annotations

from typing import Any, Dict


ALLOWED_RATIOS = {"16:9", "9:16", "1:1"}
ALLOWED_QUALITY_TARGETS = {"standard", "high"}
MAX_VIDEO_DURATION_SECONDS = 15
MIN_VIDEO_DURATION_SECONDS = 4


def validate_generation_spec(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("generation_spec must be an object")
    duration = value.get("duration_seconds")
    ratio = value.get("aspect_ratio")
    quality = value.get("quality_target")
    rationale = value.get("rationale")
    if not isinstance(duration, int) or isinstance(duration, bool) or not MIN_VIDEO_DURATION_SECONDS <= duration <= MAX_VIDEO_DURATION_SECONDS:
        raise ValueError(
            f"generation_spec.duration_seconds must be a whole number from {MIN_VIDEO_DURATION_SECONDS} to {MAX_VIDEO_DURATION_SECONDS}"
        )
    if ratio not in ALLOWED_RATIOS:
        raise ValueError("generation_spec.aspect_ratio must be 16:9, 9:16, or 1:1")
    if quality not in ALLOWED_QUALITY_TARGETS:
        raise ValueError("generation_spec.quality_target must be standard or high")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("generation_spec.rationale must be non-empty text")
    return {
        "duration_seconds": duration,
        "aspect_ratio": ratio,
        "quality_target": quality,
        "rationale": rationale.strip(),
    }


def fallback_generation_spec(
    prompt: str,
    reference_asset: Dict[str, Any] | None = None,
    *,
    duration_override: int | None = None,
) -> Dict[str, Any]:
    """A compact fallback that honours explicit user duration before inference.

    This path runs only when the live planning model is unavailable.  It must
    not reintroduce the former 5/10-second test-model policy.
    """
    brief = (prompt or "").lower()
    vertical_terms = ("vertical", "portrait", "reel", "shorts", "tiktok", "抖音", "竖屏")
    square_terms = ("square", "1:1", "feed", "方形")
    narrative_terms = ("three shot", "three-shot", "story", "sequence", "narrative", "三镜", "故事", "连续", "先", "然后", "最后")
    detail_terms = ("logo", "product", "detail", "close-up", "commercial", "广告", "产品", "细节", "特写")
    motion_terms = ("run", "dance", "jump", "skate", "drive", "unfold", "transform", "chase", "walk", "跑", "跳", "舞", "滑", "开", "变形", "追", "移动")
    multi_action_terms = ("then", "after", "before", "while", "and then", "followed by", "finally", "接着", "随后", "之后", "最后", "同时", "再")
    ratio = "9:16" if any(term in brief for term in vertical_terms) else "1:1" if any(term in brief for term in square_terms) else "16:9"
    requested_duration = requested_duration_seconds(prompt)
    if any(duration > MAX_VIDEO_DURATION_SECONDS for duration in _requested_duration_values(prompt)):
        raise ValueError(f"Requested video duration exceeds the {MAX_VIDEO_DURATION_SECONDS}-second limit")
    duration = duration_override or requested_duration or _infer_fallback_duration(
        brief, narrative_terms, motion_terms, multi_action_terms, detail_terms,
    )
    if not MIN_VIDEO_DURATION_SECONDS <= duration <= MAX_VIDEO_DURATION_SECONDS:
        raise ValueError(f"Requested video duration must be from {MIN_VIDEO_DURATION_SECONDS} to {MAX_VIDEO_DURATION_SECONDS} seconds")
    quality = "high" if reference_asset or any(term in brief for term in detail_terms) else "standard"
    duration_reason = (
        "the duration explicitly requested in the creative brief"
        if requested_duration else
        _fallback_duration_reason(duration)
    )
    reason = (
        f"Selected {ratio} for the requested viewing context, {duration}s for {duration_reason}, and "
        f"{quality} clarity because {'a visual reference or fine detail must be preserved' if quality == 'high' else 'the brief prioritises a focused first version'}."
    )
    return {"duration_seconds": duration, "aspect_ratio": ratio, "quality_target": quality, "rationale": reason}


def _infer_fallback_duration(
    brief: str,
    narrative_terms: tuple[str, ...],
    motion_terms: tuple[str, ...],
    multi_action_terms: tuple[str, ...],
    detail_terms: tuple[str, ...],
) -> int:
    """Infer a small but non-constant duration when live planning is down.

    The fallback cannot see a true semantic shot plan.  It therefore uses only
    observable textual complexity and never pretends to make a model-quality
    creative judgement.  The live planner remains the preferred path.
    """
    has_narrative = any(term in brief for term in narrative_terms)
    multi_action_count = sum(brief.count(term) for term in multi_action_terms)
    has_motion = any(term in brief for term in motion_terms)
    has_detail = any(term in brief for term in detail_terms)
    if multi_action_count >= 2:
        return 12
    if has_narrative:
        return 10
    if has_motion:
        return 8
    if has_detail:
        return 6
    return 5


def _fallback_duration_reason(duration: int) -> str:
    reasons = {
        5: "a single, focused visual beat with no stated action chain",
        6: "one focused product or detail beat that needs readable screen time",
        8: "one dynamic action that needs visible setup, motion, and completion",
        10: "a short multi-beat sequence with setup, key action, and outcome",
        12: "a stated multi-step narrative that needs paced, inspectable beats",
    }
    return reasons.get(duration, "the inferred observable complexity of the creative brief")


def requested_duration_seconds(prompt: str) -> int | None:
    """Return an explicit whole-second request from Chinese or English brief text."""
    values = _requested_duration_values(prompt)
    return values[0] if values else None


def _requested_duration_values(prompt: str) -> list[int]:
    """Extract every explicit duration so a later value cannot bypass policy."""
    import re

    text = (prompt or "").lower()
    pattern = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:-\s*)?(seconds?|secs?|s|秒|minutes?|mins?|分钟|分)(?![a-z])")
    values = []
    for match in pattern.finditer(text):
        amount = float(match.group(1))
        unit = match.group(2)
        values.append(max(1, round(amount * 60 if unit.startswith("m") or unit in {"分钟", "分"} else amount)))
    return values


def validate_brief_duration(prompt: str) -> None:
    """Reject an over-limit request without altering the user's original brief."""
    durations = _requested_duration_values(prompt)
    if any(duration < MIN_VIDEO_DURATION_SECONDS for duration in durations):
        raise ValueError(f"Video duration must be at least {MIN_VIDEO_DURATION_SECONDS} seconds")
    if any(duration > MAX_VIDEO_DURATION_SECONDS for duration in durations):
        raise ValueError(f"Video duration cannot exceed {MAX_VIDEO_DURATION_SECONDS} seconds")
