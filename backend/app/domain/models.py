"""Domain objects for the Video Control Loop Agent.

They use dataclasses instead of framework schemas so the demo runs with Python's
standard library only. Every object is serialised to JSON for an auditable log.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"


class Action(str, Enum):
    ACCEPT_AND_CONTINUE = "accept_and_continue"
    REWRITE_PROMPT = "rewrite_prompt"
    INJECT_CONTINUITY = "inject_continuity_state"
    ADD_REFERENCE = "add_reference_constraint"
    SPLIT_SHOT = "split_shot"
    REGENERATE_SHOT = "regenerate_shot"
    STOP_AND_REPORT = "stop_and_report"


@dataclass
class RubricScores:
    instruction_following: float
    character_consistency: float
    camera_control: float
    visual_quality: float
    narrative_continuity: float
    audio_visual_alignment: float

    def overall(self) -> float:
        # Audio is explicitly out of scope for the visual-only Rubric v1.1.
        values = [
            self.instruction_following,
            self.character_consistency,
            self.camera_control,
            self.visual_quality,
            self.narrative_continuity,
        ]
        return round(sum(values) / len(values), 2)

    def weighted_overall(self, weights: Dict[str, float] | None = None) -> float:
        """Diagnostic aggregate only — publication never relies on this number."""
        values = {
            "instruction_following": self.instruction_following,
            "character_consistency": self.character_consistency,
            "camera_control": self.camera_control,
            "visual_quality": self.visual_quality,
            "narrative_continuity": self.narrative_continuity,
        }
        weights = weights or {key: 1 / len(values) for key in values}
        total = sum(float(weights.get(key, 0)) for key in values)
        if total <= 0:
            return self.overall()
        return round(sum(values[key] * float(weights.get(key, 0)) for key in values) / total, 2)

    def as_dict(self) -> Dict[str, float]:
        result = asdict(self)
        result["overall"] = self.overall()
        return result


@dataclass
class BadCase:
    issue_type: str
    shot_id: str
    severity: Severity
    evidence: str
    root_cause_hypothesis: str
    affected_dimensions: List[str]


@dataclass
class EvaluationReport:
    version_id: str
    scores: RubricScores
    bad_cases: List[BadCase]
    summary: str
    atomic_checks: List[Dict[str, Any]] = field(default_factory=list)
    release_decision: str = "insufficient_evidence"
    evidence_coverage: Dict[str, int] = field(default_factory=dict)
    # Per-dimension evidence counts make a score inspectable without turning
    # an unobserved dimension into a fabricated rating.
    dimension_evidence: Dict[str, Dict[str, int]] = field(default_factory=dict)
    observation_source: str = "fixture"
    observation_notice: str = ""
    rubric_profile: Dict[str, Any] = field(default_factory=dict)
    diagnostic_overall: float = 0.0


@dataclass
class CreativeSpec:
    goal: str
    immutable_character_attributes: List[str]
    # Semantic beats, not a promise of literal hard cuts. Compiled beats also
    # carry duration/start/end/critical_moment timing fields.
    shots: List[Dict[str, Any]]
    source_prompt: str = ""
    max_iterations: int = 2
    reference_asset: Optional[Dict[str, str]] = None
    # `reference_asset` remains the primary image used by the visual planning
    # and QA path. `multimodal_assets` retains ordered user materials for
    # planning, provider capability routing, and later quality evaluation.
    multimodal_assets: List[Dict[str, str]] = field(default_factory=list)
    # Metadata-only ledger of every asset, its input order, and any explicit
    # @ association made in the user's brief.
    asset_manifest: List[Dict[str, Any]] = field(default_factory=list)
    recommended_iterations: int = 0
    planning_rationale: str = ""
    risk_forecast: List[str] = field(default_factory=list)
    evidence_sampling: Dict[str, Any] = field(default_factory=dict)
    generation_spec: Dict[str, Any] = field(default_factory=dict)
    video_model_selection: Dict[str, Any] = field(default_factory=dict)
    # The semantic pacing plan is the source of truth for the provider-bound
    # duration and evaluation windows. `production_strategy` exposes the
    # Agent's explicit single-clip feasibility decision.
    duration_plan: Dict[str, Any] = field(default_factory=dict)
    production_strategy: Dict[str, Any] = field(default_factory=dict)
    # Agent-owned execution defaults that make an underspecified user brief
    # provider-ready. They never override immutable user constraints.
    production_direction: Dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: List[Dict[str, Any]] = field(default_factory=list)
    # Pre-generation facts and bounded tool observations.  This is intentionally
    # separate from the user-visible brief and from the version-level memory.
    enriched_context: Dict[str, Any] = field(default_factory=dict)
    # Prompt Compiler v1.1 keeps the executable generation/evaluation contracts
    # close to the approved control plan. They are not a user-facing prompt
    # editor; they make every provider call and later quality judgement traceable.
    control_plan_id: str = ""
    compile_run_id: str = ""
    generation_contract: Dict[str, Any] = field(default_factory=dict)
    evaluation_contract: Dict[str, Any] = field(default_factory=dict)
    rubric_profile: Dict[str, Any] = field(default_factory=dict)
    planning_source: str = "fixture"
    planning_notice: str = ""


@dataclass
class ContinuityState:
    shot_id: str
    character_state: str
    prop_state: str
    setting_state: str
    emotional_state: str


@dataclass
class Decision:
    action: Action
    target_shot: Optional[str]
    priority: str
    reason: str
    expected_improvement: List[str]
    rejected_options: List[str]
    tool_name: str


@dataclass
class RepairTask:
    task_id: str
    action: Action
    target_shot: str
    prompt: str
    continuity_constraints: List[str]
    human_handoff: str
    # Current video providers regenerate one complete clip. `target_shot` is a
    # diagnostic priority inside that clip, never a promise of local editing.
    execution_scope: str = "whole_clip_regeneration"
    scope_notice: str = "The provider will regenerate the complete clip; the target beat identifies the evidence-backed priority to improve."


@dataclass
class GenerationJob:
    job_id: str
    version_id: str
    tool_name: str
    model_id: str
    prompt: str
    provider_id: str = ""
    status: str = "queued"
    selection_reason: str = ""
    rejected_option: str = ""
    request_mode: str = ""
    capability_profile_id: str = ""
    capability_notice: str = ""
    output_video_url: str = ""
    error: str = ""
    compile_run_id: str = ""
    generation_contract_id: str = ""
    evaluation_contract_id: str = ""


@dataclass
class VersionReview:
    """Immutable review artifacts for one point in the linear video-version chain."""

    version_id: str
    parent_version_id: Optional[str]
    asset: Optional[Dict[str, str]] = None
    report: Optional[EvaluationReport] = None
    decision: Optional[Decision] = None
    repair_task: Optional[RepairTask] = None
    status: str = "awaiting_evaluation"
    observation_error: str = ""
    compile_run_id: str = ""
    generation_contract_id: str = ""
    evaluation_contract_id: str = ""


@dataclass
class ProjectState:
    project_id: str
    creative_spec: CreativeSpec
    current_version_id: str = "v1"
    current_video_asset: Optional[Dict[str, str]] = None
    evaluation_frame_records: List[Dict[str, Any]] = field(default_factory=list)
    iterations_used: int = 0
    status: str = "draft"
    continuity_memory: List[ContinuityState] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    evaluation_history: List[EvaluationReport] = field(default_factory=list)
    version_reviews: List[VersionReview] = field(default_factory=list)
    generation_jobs: List[GenerationJob] = field(default_factory=list)
    latest_report: Optional[EvaluationReport] = None
    latest_decision: Optional[Decision] = None
    latest_repair_task: Optional[RepairTask] = None
    observation_error: str = ""
    # Execution mode is a user-visible control boundary.  Autopilot may act
    # inside the approved repair budget, while `autopilot_paused` stops the
    # next action without pretending to cancel an in-flight provider job.
    execution_mode: str = "collaborative"
    autopilot_paused: bool = False
    pending_user_direction: str = ""
    # Submitted conversation turns, independent of the changing active plan.
    conversation: List[Dict[str, Any]] = field(default_factory=list)


def to_jsonable(value: Any) -> Any:
    """Convert nested dataclasses and enums to JSON-compatible values."""
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value
