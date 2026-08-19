"""Hard boundaries: the LLM can recommend, but policies decide what is legal."""

from __future__ import annotations

from ..domain.models import Action, Decision, ProjectState
from ..domain.rubric import is_publishable


ALLOWED_ACTIONS = {
    Action.ACCEPT_AND_CONTINUE,
    Action.REWRITE_PROMPT,
    Action.INJECT_CONTINUITY,
    Action.ADD_REFERENCE,
    Action.SPLIT_SHOT,
    Action.REGENERATE_SHOT,
    Action.STOP_AND_REPORT,
}


def remaining_budget(state: ProjectState) -> int:
    return max(0, state.creative_spec.max_iterations - state.iterations_used)


def must_stop(state: ProjectState) -> bool:
    return remaining_budget(state) == 0


def validate_decision(state: ProjectState, decision: Decision) -> None:
    if decision.action not in ALLOWED_ACTIONS:
        raise ValueError("Planner selected an unsupported action")
    if decision.action not in {Action.ACCEPT_AND_CONTINUE, Action.STOP_AND_REPORT}:
        if not decision.target_shot:
            raise ValueError("Repair actions need a target shot")
        if remaining_budget(state) <= 0:
            raise ValueError("Generation budget has been exhausted")


def terminal_status(state: ProjectState) -> str | None:
    if state.latest_report and is_publishable(state.latest_report):
        return "published"
    if must_stop(state):
        return "stopped_budget_exhausted"
    return None
