"""Observation tools for the Video Control Loop Agent."""

from __future__ import annotations

import base64
import json
import re
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..domain.models import BadCase, EvaluationReport, ProjectState, RubricScores, Severity
from ..domain.rubric import apply_release_decision, profile_from_state
from ..agent.asset_manifest import evaluation_reference_entries
from .audio_probe import AudioPresenceProbe
from .ark_responses import configured_model, request_json, response_text


DIMENSIONS = (
    "instruction_following",
    "character_consistency",
    "camera_control",
    "visual_quality",
    "narrative_continuity",
)


class FixtureVisionEvaluator:
    """Deterministic observation retained for the sample project and safe fallback."""

    def analyze(self, state: ProjectState) -> EvaluationReport:
        version_id = state.current_version_id
        profile = profile_from_state(state)
        if version_id == "v2":
            report = EvaluationReport(
                version_id="v2",
                scores=RubricScores(4.4, 4.3, 4.1, 4.2, 4.2, 4.0),
                bad_cases=[],
                summary=(
                    "All release gates pass. The two atomic action shots preserve the silver "
                    "necklace into the final reaction shot."
                ),
                observation_source="fixture",
                observation_notice="Deterministic sample evaluation.",
            )
            report.atomic_checks = self._fixture_checks(state, passed=True)
            return apply_release_decision(report, profile)
        report = EvaluationReport(
            version_id="v1",
            scores=RubricScores(2.4, 4.1, 3.2, 3.8, 2.2, 4.0),
            bad_cases=[
                BadCase(
                    issue_type="action_not_completed",
                    shot_id="S2",
                    severity=Severity.BLOCKING,
                    evidence="00:03–00:06: the detective bends down but does not pick up the necklace.",
                    root_cause_hypothesis="The prompt combines a four-step action chain in one short shot.",
                    affected_dimensions=["instruction_following", "narrative_continuity"],
                ),
                BadCase(
                    issue_type="prop_state_loss",
                    shot_id="S3",
                    severity=Severity.BLOCKING,
                    evidence="00:00–00:02: the detective's right hand is empty despite S2's intended pickup.",
                    root_cause_hypothesis="S3 was generated independently without an explicit prop-state constraint.",
                    affected_dimensions=["narrative_continuity", "instruction_following"],
                ),
                BadCase(
                    issue_type="camera_distance_drift",
                    shot_id="S2",
                    severity=Severity.MINOR,
                    evidence="The requested medium shot starts as a wide shot for roughly one second.",
                    root_cause_hypothesis="Scene description competes with the camera instruction.",
                    affected_dimensions=["camera_control"],
                ),
            ],
            summary=(
                "The character is stable, but the narrative turning point fails: the necklace is "
                "not picked up in S2 and cannot be carried into S3."
            ),
            observation_source="fixture",
            observation_notice="Deterministic sample evaluation.",
        )
        report.atomic_checks = self._fixture_checks(state, passed=False)
        return apply_release_decision(report, profile)

    @staticmethod
    def _fixture_checks(state: ProjectState, passed: bool) -> list[dict[str, Any]]:
        """Make the test-only fixture obey the same evidence policy as live runs."""
        checks = []
        criteria = [
            item for item in ((state.creative_spec.evaluation_contract or {}).get("acceptance_criteria") or state.creative_spec.acceptance_criteria)
            if item.get("evidence_mode") != "audio_probe"
        ]
        for index, criterion in enumerate(criteria or []):
            status = "pass" if passed or index == 0 else "fail"
            checks.append({
                **criterion,
                "status": status,
                "observed": criterion["expected"] if status == "pass" else "The planned action or outcome is not visibly complete.",
                "evidence": [{"frame_id": f"F{min(index + 1, 6)}", "timestamp": float(index + 1), "description": "Deterministic fixture evidence."}],
                "repair_hint": "Make the missing action explicit in the repair prompt." if status == "fail" else "",
            })
        return checks


class ArkVisionEvaluator:
    """Evaluate browser-sampled video keyframes with Doubao Seed 2.1 Pro."""

    endpoint = "https://ark.cn-beijing.volces.com/api/v3/responses"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("ARK_API_KEY")
        self.model = model or configured_model()

    def available(self) -> bool:
        return bool(self.api_key and self.model)

    def analyze(self, state: ProjectState) -> EvaluationReport:
        if not self.available():
            raise RuntimeError("ARK_API_KEY or ARK_MODEL is not configured")
        if not state.evaluation_frame_records:
            raise ValueError("No video keyframes are available for visual evaluation")

        content: list[dict[str, Any]] = [{"type": "input_text", "text": self._instruction(state)}]
        assets_by_order = {
            int(asset.get("input_order", index)): asset
            for index, asset in enumerate(state.creative_spec.multimodal_assets or [], start=1)
            if isinstance(asset, dict)
        }
        manifest = (state.creative_spec.evaluation_contract or {}).get("asset_manifest") or state.creative_spec.asset_manifest
        for entry in evaluation_reference_entries(manifest):
            asset = assets_by_order.get(int(entry.get("input_order", 0)), {})
            preview = asset.get("preview_data_url")
            if not (isinstance(preview, str) and preview.startswith("data:image/")):
                continue
            content.extend([
                {"type": "input_text", "text": f"Reference image {entry['asset_id']} ({entry['name']}). Use it only for acceptance criteria whose asset_bindings contains {entry['asset_id']}; do not infer details that are not visible."},
                {"type": "input_image", "image_url": preview},
            ])
        for record in state.evaluation_frame_records:
            frame_path = Path(str(record["path"]))
            image_data = base64.b64encode(frame_path.read_bytes()).decode("ascii")
            label = (
                f"{record['frame_id']} at {float(record['timestamp']):.1f}s · "
                f"planned evidence for {record.get('expected_shot_id', 'unknown shot')} · "
                f"sampling reason: {record.get('sampling_reason', 'baseline coverage')}"
            )
            content.extend([
                {"type": "input_text", "text": label},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_data}"},
            ])

        frame_shot_map = {
            str(record.get("frame_id")): {
                "shot_id": str(record.get("expected_shot_id")),
                "timestamp": float(record.get("timestamp", 0)),
            }
            for record in state.evaluation_frame_records
            if record.get("expected_shot_id") in {shot.get("shot_id") for shot in state.creative_spec.shots}
        }
        try:
            return self._evaluate_content(state, content, frame_shot_map)
        except (ValueError, json.JSONDecodeError) as first_error:
            # A provider can understand every supplied frame and still violate
            # a narrow JSON field contract (for example, return “N/A” for a
            # static camera).  That is a recoverable observation-format issue,
            # not a reason to discard an already generated video or fabricate
            # a score.  Re-observe the same evidence once with an explicit
            # correction instruction.  A second invalid result remains a
            # truthful observation failure and is surfaced to the user.
            retry_content = [*content, {
                "type": "input_text",
                "text": self._schema_retry_instruction(first_error),
            }]
            try:
                report = self._evaluate_content(state, retry_content, frame_shot_map)
            except (ValueError, json.JSONDecodeError) as retry_error:
                raise ValueError(
                    "Vision schema retry failed after an incomplete initial evaluation: "
                    f"{retry_error}"
                ) from retry_error
            report.observation_notice = (
                f"{report.observation_notice} A constrained schema retry completed using the same retained frame evidence."
            )
            return report

    def _evaluate_content(
        self,
        state: ProjectState,
        content: list[dict[str, Any]],
        frame_shot_map: dict[str, dict[str, Any]],
    ) -> EvaluationReport:
        parsed = request_json(self.api_key, self.model, "Return the requested video evaluation as JSON. Follow the evaluation contract in the supplied content.", None, 4000, timeout=90, endpoint=self.endpoint, content=content)
        return self._to_report(state, parsed, frame_shot_map)

    @staticmethod
    def _schema_retry_instruction(error: Exception) -> str:
        # Keep the correction bounded. The original prompt still contains the
        # authoritative brief, contract, and all ordered image evidence.
        reason = str(error).replace("\n", " ")[:220]
        return f"""FORMAT CORRECTION — re-evaluate the exact same supplied evidence and return a fresh JSON object only.
The previous draft failed the contract check: {reason}
All five keys in `scores` are mandatory: instruction_following, character_consistency, camera_control, visual_quality, narrative_continuity. Each value must be one JSON number from 1.0 to 5.0. If a dimension is not applicable (for example a static camera), score its visible execution using the anchor scale; never write N/A, null, text, a range, or an object. Preserve the acceptance-criteria IDs and cite only the supplied F-frame timestamps."""

    @staticmethod
    def _response_text(raw: dict[str, Any]) -> str:
        """Support the Responses API's output array without depending on an SDK."""
        return response_text(raw)

    def _instruction(self, state: ProjectState) -> str:
        spec = state.creative_spec
        evaluation_contract = spec.evaluation_contract or {}
        rubric_profile = evaluation_contract.get("rubric_profile") or profile_from_state(state)
        shots = "; ".join(
            f"{shot['shot_id']} ({shot.get('start_seconds', '?')}–{shot.get('end_seconds', '?')}s; critical moment: {shot.get('critical_moment', 'not declared')}): {shot['intent']}"
            for shot in spec.shots
        )
        constraints = "; ".join(spec.immutable_character_attributes)
        sampling_rationale = str(((evaluation_contract.get("evidence_plan") or {}).get("sampling_policy") or spec.evidence_sampling or {}).get("rationale", ""))
        visual_criteria = [
            item for item in (evaluation_contract.get("acceptance_criteria") or spec.acceptance_criteria)
            if item.get("evidence_mode") != "audio_probe"
        ]
        criteria = json.dumps(visual_criteria, ensure_ascii=False)
        return f"""You are the observation tool of an AI video quality-control agent. Evaluate the ordered keyframes from one video. The original creative brief is: {spec.source_prompt or spec.goal}
Control-plan semantic beats and expected time windows: {shots}
Immutable constraints: {constraints}
Evidence sampling policy: {sampling_rationale or "Cover each planned shot and focus on the key action."}
Acceptance criteria (the source of truth): {criteria}
Selected rubric profile: {rubric_profile.get("label", "Controlled video")}.
Profile focus: {rubric_profile.get("observation_focus", "Verify the approved requirements with frame evidence.")}
Active release dimensions: {", ".join(rubric_profile.get("release_dimensions") or DIMENSIONS)}. Only these dimensions participate in the release-score gate; other returned dimensions are diagnostic only. Do not penalise a one-beat plan for lacking cross-beat continuity, or a static plan for lacking motion, unless its acceptance criteria explicitly require them.

Score anchors: 5 = exceptional, comprehensively evidenced execution with no material visible issue; 4 = strongly supported execution with only limited evidence or a minor non-critical deviation; 3 = usable but only partially supported / noticeably imperfect; 2 = major visible mismatch; 1 = core requirement absent or unusable. A 5 is never a default for an attractive-looking frame. Give 5 to an active dimension only when the cited acceptance evidence directly supports every relevant requirement and there are at least two independent supporting observations. A single cited keyframe can support at most 4. Keyframes cannot by themselves prove smooth motion, lack of flicker, audio synchronisation, or unseen time ranges. If a dimension is inactive for this plan, return 3.0 as a neutral diagnostic value; it does not participate in the release decision. `visual_quality` must account for visible artifacts such as malformed anatomy, illegible required text, melting products, duplicate objects, or severe edge tearing. When active, `narrative_continuity` must account for visible state changes across the ordered supplied frames. `instruction_following` must reflect the actual prompt, planned beat requirements, and expected time windows, not generic visual appeal. A frame outside an expected beat window cannot prove that beat's criterion.

Return valid JSON only, with no markdown:
{{
  "scores": {{"instruction_following": 1.0, "character_consistency": 1.0, "camera_control": 1.0, "visual_quality": 1.0, "narrative_continuity": 1.0}},
  "atomic_checks": [{{"requirement_id": "copied from criterion", "status": "pass|fail|insufficient_evidence", "observed": "what the supplied frames visibly show", "evidence": [{{"frame_id": "F1", "timestamp": 1.2, "description": "visible evidence"}}], "repair_hint": "empty when pass; a concise repair instruction otherwise"}}],
  "summary": "concise release-quality summary"
}}
Every one of the five score values is required and must be a JSON number from 1 to 5, for example `"camera_control": 3.0`. Never return null, N/A, a range, a label, prose, or a nested object as a score. Audio is observed by a separate local tool: do not return audio scores or audio checks. Return exactly one atomic_checks record for every supplied visual criterion: never invent, remove, merge, or change criteria. For critical pass or fail, cite at least one supplied F-frame and its timestamp. If the keyframes cannot prove a criterion, choose insufficient_evidence rather than guessing. Do not claim anything about unseen time ranges. Only compare a supplied reference image when that criterion lists the matching R-ID in asset_bindings; an uploaded but unbound asset is not evidence for any criterion. Keep the entire JSON response under 900 words."""

    @staticmethod
    def _parse_json(value: Any) -> dict[str, Any]:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Vision model returned no evaluation")
        text = value.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("Vision evaluation did not contain JSON")
            text = text[start:end + 1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("Vision evaluation must be a JSON object")
        return parsed

    def _to_report(self, state: ProjectState, parsed: dict[str, Any], frame_shot_map: dict[str, str] | None = None) -> EvaluationReport:
        if isinstance(parsed.get("evaluation"), dict):
            parsed = parsed["evaluation"]
        raw_scores = parsed.get("scores") or parsed.get("rubric_scores")
        if not isinstance(raw_scores, dict):
            raise ValueError("Vision evaluation is missing scores")
        aliases = {
            "instruction_following": ("instruction_following", "instructionFollowing"),
            "character_consistency": ("character_consistency", "characterConsistency"),
            "camera_control": ("camera_control", "cameraControl"),
            "visual_quality": ("visual_quality", "visualQuality"),
            "narrative_continuity": ("narrative_continuity", "narrativeContinuity"),
        }
        values: dict[str, float] = {}
        for dimension in DIMENSIONS:
            value = next((raw_scores.get(key) for key in aliases[dimension] if key in raw_scores), None)
            score = self._normalise_score(value)
            if score is None:
                # Do not replace a missing score with a product-side default:
                # that would make a release decision appear evidence-backed
                # when the visual model had not actually supplied one.
                rendered = repr(value)
                raise ValueError(f"Invalid vision score for {dimension}: {rendered[:120]}")
            values[dimension] = score
        values["audio_visual_alignment"] = 0.0
        evaluation_contract = state.creative_spec.evaluation_contract or {}
        rubric_profile = evaluation_contract.get("rubric_profile") or profile_from_state(state)
        criteria = [
            item for item in (evaluation_contract.get("acceptance_criteria") or state.creative_spec.acceptance_criteria)
            if item.get("evidence_mode") != "audio_probe"
        ]
        checks = self._atomic_checks(
            criteria,
            parsed.get("atomic_checks", parsed.get("atomicChecks")),
            frame_shot_map or {},
        )
        values, calibration_notes = self._calibrate_scores_with_evidence(values, checks, rubric_profile)
        cases = self._checks_to_bad_cases(checks)
        report = EvaluationReport(
            version_id=state.current_version_id,
            scores=RubricScores(**values),
            bad_cases=cases,
            summary=self._text(parsed.get("summary", parsed.get("overall_summary")), "summary"),
            atomic_checks=checks,
            observation_source="doubao_seed_2_1_pro",
            observation_notice=(
                f"Doubao Seed 2.1 Pro evaluated {len(checks)} planned acceptance checks from browser-sampled keyframes. "
                + (" ".join(calibration_notes) if calibration_notes else "Scores retained their evidence-calibrated values.")
            ),
        )
        return apply_release_decision(report, rubric_profile)

    @staticmethod
    def _calibrate_scores_with_evidence(
        values: dict[str, float],
        checks: list[dict[str, Any]],
        rubric_profile: dict[str, Any],
    ) -> tuple[dict[str, float], list[str]]:
        """Prevent sparse keyframe evidence from being presented as a perfect score.

        The vision model remains the observer; this small deterministic layer
        only enforces the product's stated burden of proof.  A check that
        failed or lacks evidence cannot coexist with a perfect score for the
        diagnostic dimension it evaluates.  Likewise, a lone sampled frame is
        useful evidence but not sufficient proof of 5/5 execution.
        """
        active = set(rubric_profile.get("release_dimensions") or DIMENSIONS)
        calibrated = dict(values)
        notes: list[str] = []
        for dimension in active:
            if dimension not in DIMENSIONS:
                continue
            related = [item for item in checks if item.get("rubric_dimension") == dimension]
            raw_score = float(calibrated[dimension])
            cap: float | None = None
            reason = ""
            if not related:
                cap, reason = 3.0, "no mapped acceptance evidence"
            elif any(item.get("status") == "fail" for item in related):
                cap, reason = 2.0, "a mapped acceptance check failed"
            elif any(item.get("status") == "insufficient_evidence" for item in related):
                cap, reason = 3.0, "a mapped acceptance check lacks valid evidence"
            elif all(item.get("status") == "pass" and item.get("evidence") for item in related) and raw_score < 3.0:
                # A low dimension score cannot coexist with complete passing
                # checks for that same dimension.  This only restores the
                # usable floor; it never fabricates a high-quality score.
                cap, reason = 3.0, "all mapped acceptance checks passed with cited evidence"
            elif raw_score >= 5.0:
                independent_frames = {
                    str(evidence.get("frame_id"))
                    for item in related
                    for evidence in item.get("evidence", [])
                    if isinstance(evidence, dict) and evidence.get("frame_id")
                }
                if len(independent_frames) < 2:
                    cap, reason = 4.0, "one sampled frame cannot establish perfect execution"
            if cap is not None and raw_score > cap:
                calibrated[dimension] = cap
                notes.append(f"{dimension} calibrated from {raw_score:.1f} to {cap:.1f}: {reason}.")
        return calibrated, notes

    @staticmethod
    def _normalise_score(value: Any) -> float | None:
        """Accept equivalent scalar formats while rejecting ambiguous scores.

        Vision models occasionally return a score wrapper or a human-readable
        scalar such as ``4/5`` despite the JSON contract. Those forms still
        carry an unambiguous 1–5 value; ``N/A`` or a score range do not.
        """
        if isinstance(value, dict):
            for key in ("score", "value", "rating"):
                if key in value:
                    return ArkVisionEvaluator._normalise_score(value[key])
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
        elif isinstance(value, str):
            text = value.strip()
            # A range ("3-4", "3 to 4") is intentionally not evidence.
            if re.search(r"\b\d+(?:\.\d+)?\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?\b", text, re.I):
                return None
            match = re.fullmatch(r"\s*([1-5](?:\.\d+)?)\s*(?:/\s*5|分|points?)?\s*", text, re.I)
            if not match:
                return None
            numeric = float(match.group(1))
        else:
            return None
        if not 1 <= numeric <= 5:
            return None
        return round(numeric, 1)

    @staticmethod
    def _atomic_checks(criteria: list[dict[str, Any]], raw_checks: Any, frame_shot_map: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(raw_checks, list):
            raw_checks = []
        raw_by_id = {str(item.get("requirement_id")): item for item in raw_checks if isinstance(item, dict)}
        checks: list[dict[str, Any]] = []
        for criterion in criteria:
            raw = raw_by_id.get(str(criterion["requirement_id"]), {})
            status = str(raw.get("status", "insufficient_evidence"))
            if status not in {"pass", "fail", "insufficient_evidence"}:
                status = "insufficient_evidence"
            observed = str(raw.get("observed") or "No valid observation was returned for this acceptance check.").strip()
            evidence = []
            if isinstance(raw.get("evidence"), list):
                for item in raw["evidence"][:3]:
                    if not isinstance(item, dict):
                        continue
                    frame_id = str(item.get("frame_id", "")).upper()
                    timestamp = item.get("timestamp")
                    metadata = frame_shot_map.get(frame_id)
                    if isinstance(metadata, str):  # keeps direct unit tests compact
                        expected_shot, expected_timestamp = metadata, None
                    elif isinstance(metadata, dict):
                        expected_shot, expected_timestamp = metadata.get("shot_id"), metadata.get("timestamp")
                    else:
                        expected_shot, expected_timestamp = None, None
                    if expected_shot != criterion["shot_id"] or not isinstance(timestamp, (int, float)):
                        continue
                    if isinstance(expected_timestamp, (int, float)) and abs(float(timestamp) - float(expected_timestamp)) > 0.25:
                        continue
                    evidence.append({
                        "frame_id": frame_id,
                        "timestamp": round(float(timestamp), 2),
                        "description": str(item.get("description") or "Visible evidence.").strip(),
                    })
            if status in {"pass", "fail"} and not evidence:
                status = "insufficient_evidence"
                observed = "The supplied observation did not cite valid evidence for this planned shot."
            checks.append({
                **criterion,
                "status": status,
                "observed": observed,
                "evidence": evidence,
                "repair_hint": str(raw.get("repair_hint") or raw.get("repairHint") or "").strip(),
            })
        return checks

    @staticmethod
    def _checks_to_bad_cases(checks: list[dict[str, Any]]) -> list[BadCase]:
        cases = []
        for check in checks:
            if check["status"] == "pass":
                continue
            severity = Severity.BLOCKING if check["status"] == "fail" and check.get("critical") else Severity.MAJOR
            evidence = "; ".join(
                f"{item['frame_id']} at {item['timestamp']:.2f}s: {item['description']}" for item in check.get("evidence", [])
            ) or check["observed"]
            cases.append(BadCase(
                issue_type="acceptance_check_failed" if check["status"] == "fail" else "acceptance_check_unverified",
                shot_id=str(check["shot_id"]),
                severity=severity,
                evidence=evidence,
                root_cause_hypothesis=check.get("repair_hint") or "Insufficient visual evidence or a mismatch with the planned requirement.",
                affected_dimensions=[str(check.get("rubric_dimension") or "instruction_following")],
            ))
        return cases

    @staticmethod
    def _text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Vision evaluation field {field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _normalise_shot_id(value: Any, evidence: Any, frame_shot_map: dict[str, str] | None = None) -> str | None:
        """Accept harmless model formatting while keeping an auditable storyboard target.

        The model sometimes returns labels such as ``Shot 1`` or ``S2 (middle)``.
        If it only names a frame, use that frame's planned shot attribution.  The
        attribution comes from the Agent's adaptive evidence-sampling policy;
        unknown labels remain invalid so the agent never invents a repair target.
        """
        candidate = str(value or "").strip().upper()
        direct = re.search(r"(?:^|\b)(?:SHOT\s*)?S?\s*([1-6])(?:\b|$)", candidate)
        if direct:
            proposed = f"S{direct.group(1)}"
            if not frame_shot_map or any((item.get("shot_id") if isinstance(item, dict) else item) == proposed for item in frame_shot_map.values()):
                return proposed
        evidence_text = str(evidence or "").upper()
        frame = re.search(r"\bF([1-9][0-9]*)\b", evidence_text)
        if frame and frame_shot_map:
            mapped = frame_shot_map.get(f"F{frame.group(1)}")
            if isinstance(mapped, dict):
                mapped = mapped.get("shot_id")
            if isinstance(mapped, str) and re.fullmatch(r"S[1-6]", mapped):
                return mapped
        return None


class ResilientVisionEvaluator:
    """Use fixtures only for the explicit sample project, never for user videos."""

    def __init__(self, primary: ArkVisionEvaluator | None = None, fallback: FixtureVisionEvaluator | None = None):
        self.primary = primary or ArkVisionEvaluator()
        self.fallback = fallback or FixtureVisionEvaluator()
        self.audio_probe = AudioPresenceProbe()

    def analyze(self, state: ProjectState) -> EvaluationReport:
        asset_source = (state.current_video_asset or {}).get("source")
        if asset_source in {"user_upload", "agent_generated"}:
            if not self.primary.available():
                raise VisionObservationUnavailable(
                    "Doubao Seed 2.1 Pro is not configured. Add ARK_API_KEY and ARK_MODEL, then restart the local server."
                )
            if not state.evaluation_frame_records:
                raise VisionObservationUnavailable("No usable browser-sampled keyframes are available for this video.")
            try:
                return self.audio_probe.enrich_report(state, self.primary.analyze(state))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise VisionObservationUnavailable(self._safe_error_message(exc)) from exc

        # The fixture remains available exclusively through the intentional sample V1 flow.
        report = self.fallback.analyze(state)
        if state.current_video_asset and not state.evaluation_frame_records:
            report.observation_notice = "No usable keyframes were available; deterministic fallback used for this run."
        return self.audio_probe.enrich_report(state, report)

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            return f"Doubao Seed 2.1 Pro returned HTTP {exc.code}. Check the Ark model ID and API-key permissions, then try again."
        if isinstance(exc, urllib.error.URLError):
            return "The local server could not reach Doubao Seed 2.1 Pro. Check your network connection and try again."
        if isinstance(exc, json.JSONDecodeError):
            return "Doubao Seed 2.1 Pro returned an unreadable response. Try the evaluation again."
        if isinstance(exc, ValueError):
            return f"Doubao Seed 2.1 Pro returned an incomplete evaluation ({exc}). Try the evaluation again."
        return "Doubao Seed 2.1 Pro could not complete the observation. Try the evaluation again."


class VisionObservationUnavailable(RuntimeError):
    """A user video must not be evaluated using invented fixture evidence."""
