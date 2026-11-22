"""Editorial timing policy for a single controlled video clip.

The planning model decides *what* each beat needs to communicate.  This module
then makes the provider-bound timing decision deterministic and auditable.  It
prevents a convenient but unreliable ``8 seconds by default`` behaviour.

The policy is intentionally limited to one 4–15-second logical clip. A video
provider may impose a narrower executable duration set; its adapter reports
that constraint before submission rather than silently changing the approved
plan. A story that needs independent scenes or more temporal change than one
clip can carry is marked for segmentation rather than silently compressed.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .timing import MAX_BEATS, MIN_BEATS, expected_shot_ids, normalise_shot_timeline


MIN_SINGLE_CLIP_SECONDS = 4
MAX_SINGLE_CLIP_SECONDS = 15
_PACING_BY_FUNCTION = {
    "establish": {"glimpse", "readable"},
    "action": {"simple", "dynamic", "transformation"},
    "payoff": {"hold", "reaction"},
    "detail": {"glimpse", "readable", "hold"},
    "transition": {"bridge", "dynamic"},
}
_INTRINSIC_SECONDS = {
    ("establish", "glimpse"): 1.5,
    ("establish", "readable"): 2.0,
    ("action", "simple"): 2.0,
    ("action", "dynamic"): 4.0,
    ("action", "transformation"): 6.0,
    ("payoff", "hold"): 2.0,
    ("payoff", "reaction"): 3.0,
    ("detail", "glimpse"): 1.5,
    ("detail", "readable"): 2.0,
    ("detail", "hold"): 3.0,
    ("transition", "bridge"): 1.5,
    ("transition", "dynamic"): 3.0,
}


def validate_production_strategy(value: Any) -> Dict[str, str]:
    """Validate the model's single-clip feasibility decision.

    Missing data remains backward compatible for local fixture plans, but new
    live plans must make this boundary explicit in their returned JSON.
    """
    if value is None:
        return {"mode": "single_clip", "reason": "The planned semantic beats fit one controlled video clip."}
    if not isinstance(value, dict):
        raise ValueError("production_strategy must be an object")
    mode = value.get("mode")
    reason = value.get("reason")
    if mode not in {"single_clip", "needs_segmentation"}:
        raise ValueError("production_strategy.mode must be single_clip or needs_segmentation")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("production_strategy.reason must be non-empty text")
    return {"mode": mode, "reason": reason.strip()}


def fallback_beat_pacing(prompt: str) -> List[Dict[str, str]]:
    """Bounded recovery pacing when a live director-plan is unavailable.

    It is deliberately modest: this is a transparent text-only recovery path,
    not a substitute for the semantic plan from Doubao Seed 2.1 Pro.
    """
    brief = (prompt or "").lower()
    sequence_markers = ("then", "after", "before", "finally", "followed by", "先", "然后", "接着", "随后", "最后", "再")
    motion_markers = ("run", "dance", "jump", "skate", "drive", "unfold", "transform", "chase", "pour", "open", "跑", "跳", "舞", "滑", "开", "变形", "追", "倒", "展开")
    reaction_markers = ("reaction", "smile", "relief", "surprise", "celebrate", "反应", "微笑", "惊喜", "释然", "庆祝")
    transitions = sum(brief.count(marker) for marker in sequence_markers)
    if transitions >= 2:
        action = "transformation"
    elif any(marker in brief for marker in motion_markers):
        action = "dynamic"
    else:
        action = "simple"
    has_reaction = any(marker in brief for marker in reaction_markers)
    # A local recovery plan is intentionally conservative, but it still
    # avoids inventing a three-shot sequence for a one-beat product reveal.
    if action == "simple" and not has_reaction and transitions == 0:
        return [{"shot_id": "S1", "function": "detail", "pacing": "readable"}]
    if action == "dynamic" and not has_reaction and transitions == 0:
        return [{"shot_id": "S1", "function": "action", "pacing": "dynamic"}]
    beats = [
        {"shot_id": "S1", "function": "establish", "pacing": "readable"},
        {"shot_id": "S2", "function": "action", "pacing": action},
    ]
    if has_reaction or transitions:
        beats.append({"shot_id": "S3", "function": "payoff", "pacing": "reaction" if has_reaction else "hold"})
    return beats


def build_duration_plan(
    shots: Iterable[Dict[str, Any]],
    *,
    explicit_duration_seconds: int | None = None,
    source: str = "agent_inferred",
    strategy: Dict[str, str] | None = None,
    rationale: str = "",
) -> Dict[str, Any]:
    """Turn semantic beat pacing into one executable clip timeline.

    The source model selects beat functions and pacing classes.  The deterministic
    policy translates those classes into seconds, so identical plans get
    identical provider requests and evaluation windows.
    """
    strategy = validate_production_strategy(strategy)
    raw = list(shots)
    shot_ids = expected_shot_ids(len(raw))
    beat_plan: List[Dict[str, Any]] = []
    for index, (expected_id, shot) in enumerate(zip(shot_ids, raw)):
        if not isinstance(shot, dict) or shot.get("shot_id") != expected_id:
            raise ValueError("Duration planning requires ordered beat IDs S1 … S6")
        function = str(shot.get("beat_function") or shot.get("function") or _default_function(index, len(raw))).strip().lower()
        if function not in _PACING_BY_FUNCTION:
            raise ValueError(f"{expected_id} has an unsupported beat_function")
        pacing = str(shot.get("pacing") or _default_pacing(function, shot.get("intent"))).strip().lower()
        if pacing not in _PACING_BY_FUNCTION[function]:
            allowed = ", ".join(sorted(_PACING_BY_FUNCTION[function]))
            raise ValueError(f"{expected_id} pacing must be one of: {allowed}")
        reason = shot.get("timing_reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = _default_reason(function, pacing)
        beat_plan.append({
            "shot_id": expected_id,
            "function": function,
            "pacing": pacing,
            "timing_reason": reason.strip(),
            "intrinsic_seconds": _INTRINSIC_SECONDS[(function, pacing)],
        })

    if strategy["mode"] == "needs_segmentation":
        return {
            "mode": "needs_segmentation",
            "source": source,
            "reason": strategy["reason"],
            "beat_plan": beat_plan,
            "total_duration_seconds": None,
            "timeline": [],
        }

    intrinsic_total = round(sum(beat["intrinsic_seconds"] for beat in beat_plan))
    if explicit_duration_seconds is not None:
        if not MIN_SINGLE_CLIP_SECONDS <= explicit_duration_seconds <= MAX_SINGLE_CLIP_SECONDS:
            raise ValueError(f"A single video clip must be {MIN_SINGLE_CLIP_SECONDS} to {MAX_SINGLE_CLIP_SECONDS} seconds")
        total = explicit_duration_seconds
        source = "user_specified"
        allocation_reason = "The explicit duration is preserved; time is redistributed to keep the key action readable."
    else:
        if intrinsic_total > MAX_SINGLE_CLIP_SECONDS:
            return {
                "mode": "needs_segmentation",
                "source": source,
                "reason": "The planned beats need more than 15 seconds of readable screen time; splitting is safer than compressing them.",
                "beat_plan": beat_plan,
                "total_duration_seconds": None,
                "timeline": [],
            }
        total = max(MIN_SINGLE_CLIP_SECONDS, intrinsic_total)
        allocation_reason = "The total is derived from the planned beat pacing, not a default duration."

    if total < len(beat_plan):
        return {
            "mode": "needs_segmentation",
            "source": source,
            "reason": "The requested duration cannot give every planned beat a readable visual moment. Reduce beats or use a longer allowed duration.",
            "beat_plan": beat_plan,
            "total_duration_seconds": None,
            "timeline": [],
        }

    durations = _allocate(total, [beat["intrinsic_seconds"] for beat in beat_plan])
    timed_shots = []
    for shot, beat, duration in zip(raw, beat_plan, durations):
        timed_shots.append({
            "shot_id": shot["shot_id"],
            "intent": shot["intent"],
            "critical_moment": shot.get("critical_moment"),
            "duration_seconds": duration,
        })
    timeline = normalise_shot_timeline(timed_shots, total, strict=all(item.get("critical_moment") for item in timed_shots))
    for shot, beat in zip(timeline, beat_plan):
        shot["beat_function"] = beat["function"]
        shot["pacing"] = beat["pacing"]
        shot["timing_reason"] = beat["timing_reason"]

    return {
        "mode": "single_clip",
        "source": source,
        "reason": rationale.strip() or allocation_reason,
        "beat_plan": beat_plan,
        "total_duration_seconds": total,
        "timeline": timeline,
    }


def duration_plan_from_timeline(shots: Iterable[Dict[str, Any]], total_duration_seconds: int) -> Dict[str, Any]:
    """Recover a fully traceable plan for older persisted control plans."""
    raw = list(shots)
    timeline = normalise_shot_timeline(raw, total_duration_seconds, strict=True)
    beats = []
    for index, shot in enumerate(timeline):
        function = str(shot.get("beat_function") or _default_function(index, len(timeline))).lower()
        pacing = str(shot.get("pacing") or _default_pacing(function, shot["intent"])).lower()
        if pacing not in _PACING_BY_FUNCTION[function]:
            pacing = _default_pacing(function, shot["intent"])
        shot["beat_function"] = function
        shot["pacing"] = pacing
        shot["timing_reason"] = shot.get("timing_reason") or _default_reason(function, pacing)
        beats.append({"shot_id": shot["shot_id"], "function": function, "pacing": pacing, "timing_reason": shot["timing_reason"], "intrinsic_seconds": shot["duration_seconds"]})
    return {
        "mode": "single_clip", "source": "legacy_timeline", "reason": "Recovered from the approved beat timeline.",
        "beat_plan": beats, "total_duration_seconds": total_duration_seconds, "timeline": timeline,
    }


def _allocate(total: int, weights: List[float]) -> List[float]:
    """Allocate integer provider seconds while preserving the directorial priorities."""
    weight_total = sum(weights)
    raw = [total * weight / weight_total for weight in weights]
    whole = [max(1, int(value)) for value in raw]
    remaining = total - sum(whole)
    fractions = sorted(range(len(raw)), key=lambda index: raw[index] - int(raw[index]), reverse=True)
    while remaining > 0:
        for index in fractions:
            if remaining <= 0:
                break
            whole[index] += 1
            remaining -= 1
    # Short explicit durations can make min-one allocation overflow. Remove
    # from the least important outer beats before touching the central action.
    preferred_removal = list(range(len(whole)))
    if len(whole) > 2:
        preferred_removal = [0, *range(2, len(whole)), 1]
    for index in preferred_removal:
        while remaining < 0 and whole[index] > 1:
            whole[index] -= 1
            remaining += 1
    if sum(whole) != total:
        raise ValueError("Unable to allocate the requested duration across the planned visible beats")
    return [float(value) for value in whole]


def _default_function(index: int, count: int) -> str:
    if count == 1:
        return "action"
    if index == 0:
        return "establish"
    if index == count - 1:
        return "payoff"
    return "action"


def _default_pacing(function: str, intent: Any) -> str:
    text = str(intent or "").lower()
    if function == "establish":
        return "readable"
    if function == "payoff":
        return "reaction" if any(term in text for term in ("reaction", "smile", "relief", "反应", "微笑", "惊喜")) else "hold"
    if function == "detail":
        return "readable"
    if function == "transition":
        return "dynamic" if any(term in text for term in ("move", "travel", "change", "移动", "转场", "变化")) else "bridge"
    if any(term in text for term in ("then", "and then", "before", "after", "transform", "unfold", "multiple", "first", "finally", "然后", "接着", "变形", "展开", "先", "最后")):
        return "transformation"
    if any(term in text for term in ("run", "dance", "jump", "skate", "drive", "move", "perform", "跑", "跳", "舞", "滑", "移动", "表演")):
        return "dynamic"
    return "simple"


def _default_reason(function: str, pacing: str) -> str:
    return {
        ("establish", "glimpse"): "A brief visual orientation is sufficient before the action.",
        ("establish", "readable"): "The subject and setting need a readable establishing beat.",
        ("action", "simple"): "One atomic action can be verified without extended screen time.",
        ("action", "dynamic"): "The viewer needs to see setup, movement, and completed action.",
        ("action", "transformation"): "The action contains a visible state change that needs setup and payoff time.",
        ("payoff", "hold"): "The result needs a stable hold for visual verification.",
        ("payoff", "reaction"): "The outcome includes a readable reaction or emotional payoff.",
        ("detail", "glimpse"): "A short product or texture glance is sufficient for this beat.",
        ("detail", "readable"): "The required detail needs readable screen time.",
        ("detail", "hold"): "The fine detail needs a stable verification hold.",
        ("transition", "bridge"): "A brief bridge is sufficient to carry continuity into the next beat.",
        ("transition", "dynamic"): "The transition itself contains visible movement that needs verification time.",
    }[(function, pacing)]
