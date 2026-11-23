"""Persist conversation guidance separately from detailed plans and tool traces."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
import re

from .domain.models import ProjectState

REPLIES = {
    "plan_ready": "Your plan is ready in the workspace. Would you like to refine it or start generating?",
    "plan_updated": "The plan has been updated. What else would you like to adjust?",
    "plan_fallback": "Live planning is unavailable. A recovery plan is available in the workspace; you can refine the brief or try again later.",
    "context_question": "Which country or region is this video for?",
    "needs_segmentation": "This idea works better as several clips. Which part would you like to start with?",
    "direction_saved": "Your update is saved for the next version.",
}


def messages(state: ProjectState) -> list[dict]:
    if state.conversation:
        result = []
        for saved in state.conversation:
            message = dict(saved)
            if message.get('kind')=='skill_execution_failed':
                from .skill_execution import failure_message
                message['text']=failure_message(TimeoutError(),state.creative_spec.enriched_context.get('ui_language','zh')) if 'timed out' in message.get('text','') else message['text']
            if state.status in {'skill_preparation_required','skill_preparation_failed','planning_failed','awaiting_skill_clarification'} and message.get('kind') in {'creative_clarification','plan_ready','plan_updated','generation_confirmation'}:
                continue
            if message.get("role") == "assistant" and not message.get("kind"):
                text = message.get("text", "")
                # Earlier builds copied the plan into the chat. Keep stored data intact,
                # but show a short, localizable handoff instead of that duplicated plan.
                if re.search(r"(?:^|\n)S\d+: ", text) or "control plan" in text or "recovery plan" in text:
                    kind = "plan_fallback" if "recovery plan" in text else "plan_ready"
                elif "multi-segment" in text or "segmentation" in text:
                    kind = "needs_segmentation"
                elif "Direction saved" in text:
                    kind = "direction_saved"
                else:
                    # Unknown historic replies remain original records, not invented questions.
                    result.append(message)
                    continue
                message.update(kind=kind, text=REPLIES[kind])
            result.append(message)
        return result
    prompt = state.creative_spec.source_prompt.strip()
    recovered = [text.strip() for text in prompt.split("\n\nAdditional user direction: ") if text.strip()]
    pending = state.pending_user_direction.strip()
    if pending and pending not in recovered:
        recovered.append(pending)
    return [{"id": f"retained-{index}", "role": "user", "text": text} for index, text in enumerate(recovered)]


def record_exchange(state: ProjectState, text: str, previous: ProjectState | None = None, reply: str | None = None) -> None:
    prior = ([dict(item) for item in previous.conversation] if previous.conversation else messages(previous)) if previous is not None else []
    if state.status=='awaiting_skill_clarification':
        kind='planning_incomplete';reply=state.creative_spec.enriched_context['skill_clarification']['question']
    elif state.status in {'skill_preparation_required','skill_preparation_failed','planning_failed'}:
        kind='planning_incomplete';reply=state.creative_spec.planning_notice
    elif reply is not None:
        kind = "direction_saved"
    elif state.status == "awaiting_context_confirmation":
        kind = "context_question"
    elif state.status == "needs_segmentation":
        kind = "needs_segmentation"
    elif state.creative_spec.planning_source == "local_fallback" or "recovery plan" in state.creative_spec.planning_notice:
        kind = "plan_fallback"
    else:
        kind = "plan_updated" if previous and previous.status != "awaiting_context_confirmation" else "plan_ready"
    timestamp = datetime.now(timezone.utc).isoformat()
    state.conversation = [*prior, {"id": uuid4().hex, "at": timestamp, "role": "user", "text": text},
                          {"id": uuid4().hex, "at": timestamp, "role": "assistant", "kind": kind, "text": reply if kind=='planning_incomplete' else REPLIES[kind]}]
