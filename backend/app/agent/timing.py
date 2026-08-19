"""Executable timing plans for short controlled videos.

The planning model chooses the semantic beats required by a video. This module
validates their durations as one coherent provider request and exposes the
same time windows to generation and evaluation.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


MIN_BEATS = 1
MAX_BEATS = 6
# Compatibility/export convenience only. A particular plan uses the prefix of
# this tuple, rather than being forced to contain all five beats.
SHOT_IDS = tuple(f"S{index}" for index in range(1, MAX_BEATS + 1))


def expected_shot_ids(count: int) -> tuple[str, ...]:
    if not MIN_BEATS <= count <= MAX_BEATS:
        raise ValueError(f"Timeline needs {MIN_BEATS} to {MAX_BEATS} ordered beats")
    return tuple(f"S{index}" for index in range(1, count + 1))


def normalise_shot_timeline(
    shots: Iterable[Dict[str, Any]], total_duration_seconds: int, *, strict: bool = False,
) -> List[Dict[str, Any]]:
    """Return an ordered contiguous timeline whose beat durations equal total.

    `strict=True` is used for live planner output: every beat needs a duration
    and a visible critical moment. The non-strict path keeps old and fallback
    plans executable by allocating a transparent default timeline.
    """
    if not isinstance(total_duration_seconds, int) or isinstance(total_duration_seconds, bool) or total_duration_seconds <= 0:
        raise ValueError("Timeline needs a positive whole-number total duration")
    raw = list(shots)
    shot_ids = expected_shot_ids(len(raw))
    for expected_id, shot in zip(shot_ids, raw):
        if not isinstance(shot, dict) or shot.get("shot_id") != expected_id:
            raise ValueError("Timeline beats must use ordered IDs S1 … S6")
        if not isinstance(shot.get("intent"), str) or not shot["intent"].strip():
            raise ValueError("Each timeline beat needs a visible intent")

    raw_durations = [shot.get("duration_seconds") for shot in raw]
    supplied = [value is not None for value in raw_durations]
    if any(supplied) and not all(supplied):
        raise ValueError("Either every timeline beat has a duration or none do")
    if strict and not all(supplied):
        raise ValueError("Live planner timeline requires a duration for every beat")
    if all(supplied):
        durations = [_positive_number(value, "shot.duration_seconds") for value in raw_durations]
        if abs(sum(durations) - total_duration_seconds) > 0.05:
            raise ValueError("Timeline beat durations must add up to generation_spec.duration_seconds")
    else:
        durations = _fallback_durations(total_duration_seconds, len(raw))

    timeline: List[Dict[str, Any]] = []
    cursor = 0.0
    for index, (shot, duration) in enumerate(zip(raw, durations), start=1):
        critical_moment = shot.get("critical_moment")
        if strict and (not isinstance(critical_moment, str) or not critical_moment.strip()):
            raise ValueError("Live planner timeline requires a critical_moment for every beat")
        end = cursor + duration
        compiled = {
            "shot_id": shot["shot_id"],
            "intent": shot["intent"].strip(),
            "duration_seconds": _round(duration),
            "start_seconds": _round(cursor),
            "end_seconds": _round(total_duration_seconds if index == len(raw) else end),
            "critical_moment": critical_moment.strip() if isinstance(critical_moment, str) and critical_moment.strip() else _default_moment(shot["shot_id"]),
        }
        # Editorial metadata is not used to calculate boundaries here, but it
        # must travel with the executable timeline so the UI and audit record
        # can explain why a beat received its allocated screen time.
        for key in ("beat_function", "pacing", "timing_reason"):
            if isinstance(shot.get(key), str) and shot[key].strip():
                compiled[key] = shot[key].strip()
        timeline.append(compiled)
        cursor = end
    # Avoid a floating-point tail in downstream contracts.
    timeline[-1]["duration_seconds"] = _round(total_duration_seconds - float(timeline[-1]["start_seconds"]))
    return timeline


def _fallback_durations(total: int, count: int) -> List[float]:
    """Distribute legacy plans without imposing a three-shot template."""
    if count == 1:
        return [float(total)]
    weights = [1.0] * count
    # A central beat normally carries the most visible action. This is only a
    # recovery allocation; live plans use the explicit pacing policy.
    weights[(count - 1) // 2] = 1.6
    weight_total = sum(weights)
    values = [round(total * weight / weight_total, 2) for weight in weights]
    values[-1] = round(total - sum(values[:-1]), 2)
    return values


def _positive_number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
        raise ValueError(f"{field} must be a positive number")
    return float(value)


def _default_moment(shot_id: str) -> str:
    index = int(shot_id[1:]) if shot_id.startswith("S") and shot_id[1:].isdigit() else 1
    if index == 1:
        return "Verify the required subject, setting, and initial visual state."
    return "Verify this planned visual beat and its required carried-forward state."


def _round(value: float) -> float:
    return round(float(value), 2)
