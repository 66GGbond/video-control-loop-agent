"""Evidence sampling bound to the Agent-selected storyboard beats."""

from __future__ import annotations

from typing import Any, Dict, List, Set

from .timing import MAX_BEATS, normalise_shot_timeline


def _shot_ids(shots: List[Dict[str, Any]]) -> list[str]:
    return [str(shot.get("shot_id")) for shot in shots if isinstance(shot, dict)]


def fallback_evidence_sampling_policy(shots: List[Dict[str, Any]], risks: List[str]) -> Dict[str, Any]:
    """Choose evidence from actual planned beats, never fixed S1/S2/S3 slots."""
    if any(not isinstance(shot, dict) or "start_seconds" not in shot or "end_seconds" not in shot for shot in shots):
        shots = normalise_shot_timeline(shots, max(4, len(shots)))
    ids = _shot_ids(shots)
    if not ids:
        raise ValueError("Evidence sampling needs a storyboard")
    joined_risks = " ".join(risks).lower()
    action_shots = [shot["shot_id"] for shot in shots if str(shot.get("beat_function", "")).lower() in {"action", "transition"}]
    focus = action_shots[:2] or [ids[-1]]
    if any(term in joined_risks for term in ("continuity", "consistency", "prop", "character", "product")) and ids[-1] not in focus:
        focus.append(ids[-1])
    focus = focus[:3]
    moments = []
    for shot_id in focus:
        intent = next((str(item.get("intent", "")) for item in shots if item.get("shot_id") == shot_id), "planned beat")
        moments.append({"shot_id": shot_id, "reason": f"Verify the visible requirement in {shot_id}: {intent[:130]}"})
    return bind_sampling_to_timeline({
        "focus_shot_ids": focus,
        "critical_moments": moments,
        "rationale": "Prioritise evidence from the planned action, transition, and highest continuity-risk beats.",
        "source": "agent_policy",
    }, shots)


def bind_sampling_to_timeline(value: Dict[str, Any], shots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach exact storyboard windows before client-side evidence sampling."""
    ids = _shot_ids(shots)
    policy = validate_evidence_sampling_policy(value, allowed_shot_ids=set(ids))
    windows = []
    for shot in shots:
        if not isinstance(shot, dict) or shot.get("shot_id") not in ids:
            raise ValueError("Timeline sampling needs valid ordered storyboard beats")
        start = _time_value(shot.get("start_seconds"), "shot.start_seconds")
        end = _time_value(shot.get("end_seconds"), "shot.end_seconds")
        if end <= start:
            raise ValueError("Timeline sampling windows must have an end after start")
        windows.append({"shot_id": shot["shot_id"], "start_seconds": start, "end_seconds": end})
    if [item["shot_id"] for item in windows] != ids:
        raise ValueError("Timeline sampling windows must follow storyboard order")
    window_map = {item["shot_id"]: item for item in windows}
    moments = [{**moment, "time_range_seconds": [window_map[moment["shot_id"]]["start_seconds"], window_map[moment["shot_id"]]["end_seconds"]]} for moment in policy["critical_moments"]]
    return {
        **policy,
        "critical_moments": moments,
        "shot_windows": windows,
        "planned_duration_seconds": max(item["end_seconds"] for item in windows),
    }


def validate_evidence_sampling_policy(
    value: Any, *, field: str = "evidence_sampling", allowed_shot_ids: Set[str] | None = None,
) -> Dict[str, Any]:
    """Validate planner output against the IDs of this exact storyboard."""
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    allowed = allowed_shot_ids or {f"S{index}" for index in range(1, MAX_BEATS + 1)}
    if not allowed:
        raise ValueError(f"{field} needs at least one allowed storyboard beat")
    maximum = min(3, len(allowed))
    raw_focus = value.get("focus_shot_ids")
    if not isinstance(raw_focus, list) or not 1 <= len(raw_focus) <= maximum:
        raise ValueError(f"{field}.focus_shot_ids must contain 1 to {maximum} shot IDs")
    focus: List[str] = []
    for shot_id in raw_focus:
        if not isinstance(shot_id, str) or shot_id not in allowed or shot_id in focus:
            raise ValueError(f"{field}.focus_shot_ids contains an invalid shot ID")
        focus.append(shot_id)
    raw_moments = value.get("critical_moments")
    if not isinstance(raw_moments, list) or not 1 <= len(raw_moments) <= maximum:
        raise ValueError(f"{field}.critical_moments must contain 1 to {maximum} items")
    moments: List[Dict[str, Any]] = []
    for item in raw_moments:
        if not isinstance(item, dict):
            raise ValueError(f"{field}.critical_moments items must be objects")
        shot_id, reason = item.get("shot_id"), item.get("reason")
        if shot_id not in focus or not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 180:
            raise ValueError(f"{field}.critical_moments contains invalid data")
        moment: Dict[str, Any] = {"shot_id": shot_id, "reason": reason.strip()}
        if item.get("time_range_seconds") is not None:
            time_range = item["time_range_seconds"]
            if not isinstance(time_range, list) or len(time_range) != 2:
                raise ValueError(f"{field}.critical_moments time range is invalid")
            start, end = _time_value(time_range[0], f"{field}.critical_moments start"), _time_value(time_range[1], f"{field}.critical_moments end")
            if end <= start:
                raise ValueError(f"{field}.critical_moments time range must increase")
            moment["time_range_seconds"] = [start, end]
        moments.append(moment)
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale.strip()) > 240:
        raise ValueError(f"{field}.rationale must be concise text")
    result: Dict[str, Any] = {"focus_shot_ids": focus, "critical_moments": moments, "rationale": rationale.strip(), "source": "doubao_seed_2_1_pro"}
    raw_windows = value.get("shot_windows")
    if raw_windows is not None:
        if not isinstance(raw_windows, list) or len(raw_windows) != len(allowed):
            raise ValueError(f"{field}.shot_windows must cover every planned storyboard beat")
        windows = []
        for item in raw_windows:
            if not isinstance(item, dict) or item.get("shot_id") not in allowed:
                raise ValueError(f"{field}.shot_windows has an invalid shot")
            start, end = _time_value(item.get("start_seconds"), f"{field}.shot_windows start"), _time_value(item.get("end_seconds"), f"{field}.shot_windows end")
            if end <= start:
                raise ValueError(f"{field}.shot_windows must increase")
            windows.append({"shot_id": item["shot_id"], "start_seconds": start, "end_seconds": end})
        if {item["shot_id"] for item in windows} != allowed:
            raise ValueError(f"{field}.shot_windows must cover each planned beat once")
        result["shot_windows"] = windows
        if value.get("planned_duration_seconds") is not None:
            result["planned_duration_seconds"] = _time_value(value["planned_duration_seconds"], f"{field}.planned_duration_seconds")
    return result


def _time_value(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return round(float(value), 2)
