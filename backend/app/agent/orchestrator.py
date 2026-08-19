"""The explicit Reason → Act → Observe loop."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from ..domain.models import Action, Decision, ProjectState, VersionReview, to_jsonable
from ..domain.rubric import is_publishable
from ..tools.evaluator import VisionObservationUnavailable
from ..tools.repair import build_continuity_memory, rewrite_generation_task
from .policies import remaining_budget, terminal_status, validate_decision


class AgentOrchestrator:
    def __init__(self, evaluator, planner, fallback_planner=None, prompt_compiler=None):
        self.evaluator = evaluator
        self.planner = planner
        self.fallback_planner = fallback_planner
        self.prompt_compiler = prompt_compiler

    def run_once(self, state: ProjectState) -> ProjectState:
        # Observe
        review = self._current_review(state)
        report = review.report
        if report is None:
            try:
                report = self.evaluator.analyze(state)
            except VisionObservationUnavailable as exc:
                state.latest_report = None
                state.latest_decision = None
                state.latest_repair_task = None
                state.observation_error = str(exc)
                review.observation_error = state.observation_error
                review.status = "observation_unavailable"
                state.status = "ready_for_evaluation"
                self._record(state, "observe", "observation_unavailable", {"message": state.observation_error})
                # A schema/network/evidence failure is not permission to keep
                # spending on repeated observations.  The user can inspect the
                # retained version and explicitly resume after the issue is
                # resolved; a normal evidence-backed repair remains automatic.
                if state.execution_mode == "autopilot" and not state.autopilot_paused:
                    state.autopilot_paused = True
                    self._record(state, "human_approval", "autopilot_paused_for_observation_failure", {
                        "message": "Agent autonomy paused because visual observation was unavailable. No additional evaluation or generation will run until the user resumes it.",
                    })
                return state
            if state.current_version_id == "none" and report.version_id != "none":
                # Keeps the zero-dependency sample fixture aligned with the same V1 → Vn chain as uploads.
                state.current_version_id = report.version_id
                review.version_id = report.version_id
            # A quality trajectory is built only from immutable, version-level observations.
            state.evaluation_history.append(report)
            review.report = report
            review.observation_error = ""
            review.status = "evaluated"
            self._record(state, "observe", "evaluation_completed", {
                "version": report.version_id,
                # This is the same scoped, evidence-calibrated diagnostic
                # shown in Version Review — never the legacy arithmetic mean
                # across irrelevant dimensions.
                "overall_score": report.diagnostic_overall,
            })
        else:
            self._record(state, "observe", "evaluation_reused", {
                "version": report.version_id,
                "overall_score": report.diagnostic_overall,
                "message": "Reused the immutable evaluation snapshot for this video version.",
            })
        state.latest_report = report
        state.observation_error = ""
        state.continuity_memory = build_continuity_memory(state, report)

        # A missing frame citation is not a creative defect.  Preserve the
        # observation, show it in the audit trail, and ask for better evidence
        # instead of fabricating a repair action.
        if report.release_decision == "insufficient_evidence":
            state.status = "ready_for_evaluation"
            state.latest_decision = None
            state.latest_repair_task = None
            review.status = "evidence_insufficient"
            review.decision = None
            review.repair_task = None
            self._record(state, "observe", "evidence_insufficient", {
                "message": "Critical acceptance criteria were not all supported by valid frame evidence.",
                "coverage": report.evidence_coverage,
            })
            return state

        terminal = terminal_status(state)
        if terminal == "published":
            state.status = terminal
            state.latest_decision = None
            state.latest_repair_task = None
            review.status = "published"
            review.decision = None
            review.repair_task = None
            self._record(state, "observe", "publish", {
                "reason": "All rubric release gates pass.",
                "remaining_budget": remaining_budget(state),
            })
            return state

        if terminal == "stopped_budget_exhausted":
            state.status = terminal
            review.status = "stopped_budget_exhausted"
            self._record(state, "observe", "stop", {
                "reason": "Budget exhausted before quality gate was met.",
                "remaining_budget": 0,
            })
            return state

        # Reason
        try:
            decision = self.planner.decide(state, report)
            validate_decision(state, decision)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            if not self.fallback_planner:
                raise
            decision = self.fallback_planner.decide(state, report)
            validate_decision(state, decision)
            self._record(state, "reason", "planner_fallback", {
                "message": "The live repair planner was unavailable; bounded fallback policy selected the repair action.",
            })
        state.latest_decision = decision
        review.decision = decision
        self._record(state, "reason", decision.action.value, to_jsonable(decision))

        # Act: build a bounded generation task.  The server submits it to the configured
        # video-generation tool, while manual upload remains an explicit fallback path.
        task = rewrite_generation_task(state, decision)
        state.pending_user_direction = ""
        state.latest_repair_task = task
        review.repair_task = task
        if self.prompt_compiler:
            self.prompt_compiler.compile(state, task, self._next_version_id(state.current_version_id))
        review.status = "repair_required"
        state.iterations_used += 1
        state.status = "awaiting_repair_upload"
        self._record(state, "act", task.task_id, to_jsonable(task))
        return state

    def simulate_repaired_upload(self, state: ProjectState) -> ProjectState:
        if state.status != "awaiting_repair_upload":
            raise ValueError("A repaired upload is only valid after an active repair task")
        if (state.current_video_asset or {}).get("source") == "user_upload":
            raise ValueError("Upload an actual repaired video for a user project; simulation is reserved for the sample flow")
        previous_version = state.current_version_id
        state.current_version_id = self._next_version_id(previous_version)
        current_asset = state.current_video_asset or {}
        state.current_video_asset = {
            **current_asset,
            "name": f"Prepared sample {state.current_version_id.upper()}",
            "source": "sample",
        }
        state.version_reviews.append(VersionReview(
            version_id=state.current_version_id,
            parent_version_id=previous_version,
            asset=state.current_video_asset.copy(),
        ))
        state.status = "repair_uploaded"
        self._record(state, "act", "manual_upload", {
            "message": f"User loaded the prepared repaired sequence as {state.current_version_id}.",
        })
        return state

    def resume_after_human_approval(self, state: ProjectState) -> ProjectState:
        """Resume Reason → Act from an immutable failed review after new approval.

        This deliberately reuses the existing report instead of observing the
        same video again. A new budget authorises a new action, not a rewrite of
        Vn's historical score or evidence.
        """
        if state.status != "stopped_budget_exhausted":
            raise ValueError("Additional repair approval is only available after the current budget is exhausted")
        if remaining_budget(state) <= 0:
            raise ValueError("Approve at least one additional repair round before resuming the Agent")
        if not state.latest_report:
            raise ValueError("The Agent needs an evaluated video before it can plan an additional repair")
        self._record(state, "human_approval", "additional_repair_authorized", {
            "message": "User approved an additional repair round; Agent will reuse the current evidence snapshot to plan the next action.",
            "remaining_budget": remaining_budget(state),
        })
        state.status = "repair_uploaded"
        return self.run_once(state)

    def prepare_optional_iteration(self, state: ProjectState) -> ProjectState:
        """Create a user-requested next version after the current one passes.

        The current review remains publishable and its score remains immutable.
        The new task is a separately auditable optional optimisation, not a
        retroactive rewrite of the passed evaluation.
        """
        if state.status != "published":
            raise ValueError("Optional iteration is only available after the current version passes the release gate")
        if remaining_budget(state) <= 0:
            raise ValueError("Approve an additional optional iteration before continuing")
        review = self._current_review(state)
        report = review.report or state.latest_report
        if not report:
            raise ValueError("The Agent needs a completed evaluation before it can optimise a published version")

        score_by_dimension = {
            "instruction_following": report.scores.instruction_following,
            "character_consistency": report.scores.character_consistency,
            "camera_control": report.scores.camera_control,
            "visual_quality": report.scores.visual_quality,
            "narrative_continuity": report.scores.narrative_continuity,
        }
        weakest_dimension = min(score_by_dimension, key=score_by_dimension.get)
        storyboard = state.creative_spec.shots
        first_shot = storyboard[0]["shot_id"]
        last_shot = storyboard[-1]["shot_id"]
        action_shot = next((shot["shot_id"] for shot in storyboard if shot.get("beat_function") in {"action", "transition"}), first_shot)
        target_by_dimension = {
            "instruction_following": action_shot,
            "character_consistency": action_shot,
            "camera_control": first_shot,
            "visual_quality": action_shot,
            "narrative_continuity": last_shot,
        }
        target_shot = target_by_dimension[weakest_dimension]
        decision = Decision(
            action=Action.REGENERATE_SHOT,
            target_shot=target_shot,
            priority="optional",
            reason=(
                "User requested an optional post-release iteration. Preserve every passed acceptance criterion "
                f"and improve {weakest_dimension.replace('_', ' ')} in {target_shot}."
            ),
            expected_improvement=[weakest_dimension],
            rejected_options=[
                "stop: the current version is already publishable, but the user explicitly requested another iteration",
                "broad rewrite: preserve the approved control plan and improve only the weakest diagnostic dimension",
            ],
            tool_name="rewrite_generation_task",
        )
        validate_decision(state, decision)
        state.latest_report = report
        state.latest_decision = decision
        review.decision = decision
        self._record(state, "human_approval", "optional_iteration_requested", {
            "message": "User requested an optional next-version optimisation after the release gate passed.",
            "source_version": review.version_id,
            "preserved_score": report.diagnostic_overall,
            "target_dimension": weakest_dimension,
        })
        self._record(state, "reason", decision.action.value, to_jsonable(decision))
        task = rewrite_generation_task(state, decision)
        state.pending_user_direction = ""
        state.latest_repair_task = task
        review.repair_task = task
        if self.prompt_compiler:
            self.prompt_compiler.compile(state, task, self._next_version_id(state.current_version_id))
        # `published` describes Vn's immutable evaluation, not V(n+1)'s task.
        state.iterations_used += 1
        state.status = "awaiting_repair_upload"
        self._record(state, "act", task.task_id, to_jsonable(task))
        return state

    @staticmethod
    def _next_version_id(version_id: str) -> str:
        if version_id.startswith("v") and version_id[1:].isdigit():
            return f"v{int(version_id[1:]) + 1}"
        return f"v{len(version_id) + 1}"

    @staticmethod
    def _current_review(state: ProjectState) -> VersionReview:
        review = next((item for item in state.version_reviews if item.version_id == state.current_version_id), None)
        if review:
            return review
        parent = state.version_reviews[-1].version_id if state.version_reviews else None
        review = VersionReview(
            version_id=state.current_version_id,
            parent_version_id=parent,
            asset=state.current_video_asset.copy() if state.current_video_asset else None,
        )
        state.version_reviews.append(review)
        return review

    def _record(self, state: ProjectState, phase: str, event: str, payload: dict) -> None:
        state.history.append({
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": phase,
            "event": event,
            "payload": payload,
        })
