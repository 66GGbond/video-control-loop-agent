"""Persist submitted messages separately from the mutable creative specification."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .domain.models import ProjectState


def messages(state: ProjectState) -> list[dict]:
    if state.conversation:
        return [dict(message) for message in state.conversation]
    # Earlier versions retained cumulative user directions in source_prompt.
    # Recover only text that still exists, never invent earlier Agent replies.
    prompt = state.creative_spec.source_prompt.strip()
    recovered = [text.strip() for text in prompt.split("\n\nAdditional user direction: ") if text.strip()]
    pending = state.pending_user_direction.strip()
    if pending and pending not in recovered:
        recovered.append(pending)
    return [{"id": f"retained-{index}", "role": "user", "text": text} for index, text in enumerate(recovered)]


def plan_reply(state: ProjectState) -> str:
    context = state.creative_spec.enriched_context
    if state.status == "awaiting_context_confirmation":
        questions = context.get("open_questions") or []
        return "\n".join(str(item.get("question", "")) for item in questions).strip() or state.creative_spec.planning_notice
    if state.status == "needs_segmentation":
        return state.creative_spec.planning_notice
    shots = state.creative_spec.shots
    # These are the actual plan outputs, including clearly labelled recovery plans.
    return "\n".join(filter(None, [state.creative_spec.planning_notice, *[
        f"{shot['shot_id']}: {shot['intent']}" for shot in shots
    ]]))


def record_exchange(state: ProjectState, text: str, previous: ProjectState | None = None, reply: str | None = None) -> None:
    prior = messages(previous) if previous is not None else []
    timestamp = datetime.now(timezone.utc).isoformat()
    state.conversation = [*prior, {"id": uuid4().hex, "at": timestamp, "role": "user", "text": text}]
    answer = plan_reply(state) if reply is None else reply
    if answer:
        state.conversation.append({"id": uuid4().hex, "at": timestamp, "role": "assistant", "text": answer})
