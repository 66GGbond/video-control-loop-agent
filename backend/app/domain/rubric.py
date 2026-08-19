"""Evidence-first release policy and bounded content-aware rubric profiles."""

from __future__ import annotations

from typing import Any, Dict

from .models import EvaluationReport, Severity


DIMENSIONS = (
    "instruction_following",
    "character_consistency",
    "camera_control",
    "visual_quality",
    "narrative_continuity",
)


def audio_evaluation_plan(spec: Any) -> Dict[str, Any]:
    """Activate only the minimum audio gate that the approved brief requires."""
    text = " ".join([
        str(getattr(spec, "source_prompt", "")), str(getattr(spec, "goal", "")),
        *[str(item.get("intent", "")) for item in getattr(spec, "shots", []) if isinstance(item, dict)],
    ]).lower()
    assets = getattr(spec, "multimodal_assets", []) or []
    has_audio_reference = any(isinstance(item, dict) and str(item.get("media_type", "")).lower() == "audio" for item in assets)
    presence_terms = ("music", "audio", "sound", "bgm", "voice", "dialogue", "lyrics", "music video", " mv", "音乐", "音频", "声音", "配乐", "口播", "台词", "歌词", "旁白", "演唱", "说唱")
    semantic_terms = ("dialogue", "voice-over", "voiceover", "spoken", "lyrics", "tagline", "台词", "口播", "歌词", "旁白", "文案")
    sync_terms = ("lip sync", "lipsync", "beat", "cut on beat", "sync", "music video", " mv", "口型", "卡点", "节拍", "音画同步", "音乐视频")
    activated = has_audio_reference or any(term in text for term in presence_terms)
    if not activated:
        return {"mode": "disabled", "active": False, "reason": "The approved brief has no material audio requirement.", "tools": []}
    mode = "sync" if any(term in text for term in sync_terms) else "semantic" if any(term in text for term in semantic_terms) else "presence"
    tools = ["audio_probe"]
    if mode == "semantic":
        tools.append("asr")
    elif mode == "sync":
        tools.extend(["beat_analysis", "lip_sync_observation"] if any(term in text for term in ("lip sync", "lipsync", "口型", "演唱", "说唱")) else ["beat_analysis"])
    reason = "An uploaded audio reference makes audio presence material to the output." if has_audio_reference else "The approved brief explicitly requests audio-led content."
    return {"mode": mode, "active": True, "reason": reason, "tools": tools}


def select_rubric_profile(spec: Any) -> Dict[str, Any]:
    """Choose a transparent local profile from approved, user-derived signals.

    This is a routing policy rather than an LLM judgement: the creative plan
    remains the source of truth, while the profile tells the observer which
    failure modes warrant the most scrutiny.
    """
    text = " ".join([
        str(getattr(spec, "source_prompt", "")), str(getattr(spec, "goal", "")),
        *[str(item) for item in getattr(spec, "immutable_character_attributes", [])],
        *[str(item.get("intent", "")) for item in getattr(spec, "shots", []) if isinstance(item, dict)],
        *[f"{item.get('requirement', '')} {item.get('expected', '')}" for item in getattr(spec, "acceptance_criteria", []) if isinstance(item, dict)],
    ]).lower()
    reference_role = str((getattr(spec, "reference_asset", None) or {}).get("role", "")).lower()
    product_terms = ("product", "logo", "brand", "packaging", "commercial", "advert", "广告", "产品", "品牌", "包装", "商品", "电商")
    action_terms = ("catch", "pick up", "unfold", "open", "pour", "throw", "jump", "run", "ride", "ski", "dance", "bite", "eat", "drink", "hold", "show", "接住", "拿起", "展开", "打开", "倒", "抛", "跳", "跑", "骑", "滑", "咬", "吃", "喝", "拿", "展示", "演示", "动作")
    music_terms = ("music video", " mv", "rap", "suno", "udio", "spotify", "音乐视频", "说唱", "歌曲", "演唱")
    if any(term in text for term in music_terms):
        return _with_evaluation_scope(_profile(
            "music_video_visual", "Music video", "The brief requests an MV, music-artist, rap, or song-led visual sequence.",
            {"instruction_following": .16, "character_consistency": .24, "camera_control": .16, "visual_quality": .20, "narrative_continuity": .24},
            ["character_consistency", "visual_quality", "narrative_continuity"],
            {"narrative_continuity": "Visual beat continuity", "camera_control": "Performance camera control"},
            "Inspect the artist or subject identity, visual beat progression and continuity across performance shots. Audio, lyrics, lip-sync and beat alignment activate separately when the approved evaluation contract requires them.",
        ), spec)
    if "product" in reference_role or any(term in text for term in product_terms):
        return _with_evaluation_scope(_profile(
            "product_ad", "Product / commercial", "The brief or reference identifies a product, brand, packaging, or commercial deliverable.",
            {"instruction_following": .28, "character_consistency": .08, "camera_control": .10, "visual_quality": .34, "narrative_continuity": .20},
            ["instruction_following", "visual_quality", "narrative_continuity"],
            {"visual_quality": "Object integrity & visual artifacts", "narrative_continuity": "Product continuity"},
            "Inspect product structure, colour, count, material and any required readable logo/text; do not let attractive lighting compensate for a product mismatch.",
        ), spec)
    if any(term in text for term in action_terms):
        return _with_evaluation_scope(_profile(
            "action_short", "Action / motion short", "The brief contains a visible physical action or object interaction.",
            {"instruction_following": .24, "character_consistency": .14, "camera_control": .30, "visual_quality": .14, "narrative_continuity": .18},
            ["instruction_following", "camera_control", "narrative_continuity"],
            {"camera_control": "Motion & physics", "narrative_continuity": "Temporal consistency"},
            "Inspect action start, contact/interaction and result. Treat impossible contact, object penetration, floating or missing completion as release-critical when required by the plan.",
        ), spec)
    return _with_evaluation_scope(_profile(
        "character_story", "Character / story", "No product-led or motion-led signal dominates the approved creative plan.",
        {"instruction_following": .24, "character_consistency": .27, "camera_control": .10, "visual_quality": .14, "narrative_continuity": .25},
        ["instruction_following", "character_consistency", "narrative_continuity"],
        {"character_consistency": "Subject / identity consistency", "narrative_continuity": "Temporal consistency"},
        "Inspect identity, wardrobe or other stated subject attributes across all planned shots, plus the causal state that carries the story between shots.",
    ), spec)


def _profile(
    profile_id: str,
    label: str,
    selection_reason: str,
    weights: Dict[str, float],
    priority_dimensions: list[str],
    label_overrides: Dict[str, str],
    observation_focus: str,
) -> Dict[str, Any]:
    labels = {
        "instruction_following": "Prompt alignment",
        "character_consistency": "Subject consistency",
        "camera_control": "Camera control",
        "visual_quality": "Visual quality",
        "narrative_continuity": "Temporal consistency",
    }
    labels.update(label_overrides)
    return {
        "rubric_version": "1.1",
        "profile_id": profile_id,
        "label": label,
        "selection_reason": selection_reason,
        "diagnostic_weights": weights,
        "priority_dimensions": priority_dimensions,
        "dimension_labels": labels,
        # 3.0/5 is the usable-release floor.  It is deliberately separate
        # from “excellent” quality: publication still requires complete
        # critical evidence and every active dimension to clear its floor.
        "overall_floor": 3.0,
        "dimension_floor": 3.0,
        "publication_policy": "Critical atomic failures and critical evidence gaps block release. Otherwise the evidence-calibrated diagnostic quality and every active visual dimension must meet their 3.0 release floor.",
        "observation_focus": observation_focus,
    }


def _with_evaluation_scope(profile: Dict[str, Any], spec: Any) -> Dict[str, Any]:
    """Bind the release gate to the actual control plan, not a fixed storyboard.

    Scores are still returned for all dimensions to support diagnostics, but a
    one-beat product reveal should not fail merely because there is no story
    transition to score. The atomic acceptance contract remains mandatory for
    every planned beat.
    """
    criteria = [item for item in getattr(spec, "acceptance_criteria", []) if isinstance(item, dict)]
    shots = [item for item in getattr(spec, "shots", []) if isinstance(item, dict)]
    active = {"instruction_following", "visual_quality"}
    if profile.get("profile_id") == "action_short":
        # A motion-led brief must never pass without a motion/physics
        # diagnostic. Individual criteria still supply the evidence; this
        # scope rule prevents a generic wording from silently excluding the
        # defining risk of the selected profile.
        active.add("camera_control")
    if profile.get("profile_id") == "music_video_visual":
        # Even a one-beat MV needs a readable performance/composition choice.
        # This gives the evaluator a third, genuinely relevant visual axis
        # without inventing cross-beat continuity for a continuous take.
        active.add("camera_control")
    for criterion in criteria:
        active.add(dimension_for_criterion(criterion, profile))
        if criterion.get("source") == "reference_image":
            active.add("character_consistency")
    if len(shots) > 1:
        # More than one approved beat creates an actual state-continuity claim.
        active.add("narrative_continuity")
    ordered = [dimension for dimension in DIMENSIONS if dimension in active]
    weights = {dimension: float(profile["diagnostic_weights"].get(dimension, 0.0)) for dimension in ordered}
    total = sum(weights.values())
    if total <= 0:
        weights = {dimension: 1 / len(ordered) for dimension in ordered}
    else:
        weights = {dimension: value / total for dimension, value in weights.items()}
    profile["release_dimensions"] = ordered
    profile["inactive_dimensions"] = [dimension for dimension in DIMENSIONS if dimension not in active]
    profile["diagnostic_weights"] = weights
    profile["evidence_policy"] = {
        "coverage_unit": "planned_semantic_beat",
        "required_beat_ids": [str(item.get("shot_id")) for item in shots],
        "critical_requirements_need_cited_frame_evidence": True,
        "unseen_content_policy": "insufficient_evidence",
    }
    return profile


def profile_from_state(state: Any) -> Dict[str, Any]:
    spec = state.creative_spec
    contract_profile = (getattr(spec, "evaluation_contract", {}) or {}).get("rubric_profile")
    return contract_profile or getattr(spec, "rubric_profile", {}) or select_rubric_profile(spec)


def dimension_for_criterion(criterion: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """Map one atomic criterion to a diagnostic dimension without modifying it."""
    text = f"{criterion.get('requirement', '')} {criterion.get('expected', '')}".lower()
    # Product appearance is not character consistency.  Keep the terminology
    # deliberately concrete here: this routing tells the release policy which
    # diagnostic score an evidence-backed acceptance check is allowed to
    # support.
    if any(term in text for term in ("logo", "brand", "product", "packaging", "structure", "material", "texture", "colour", "color", "appearance", "legible", "artifact", "logo", "文字", "品牌", "产品", "包装", "型号", "颜色", "外观", "质感", "画面", "清晰", "瑕疵", "畸形")):
        return "visual_quality"
    if any(term in text for term in ("contact", "pick", "catch", "unfold", "open", "motion", "action", "physics", "bite", "eat", "drink", "hold", "pour", "show", "接住", "拿起", "展开", "打开", "咬", "吃", "喝", "拿", "倒", "展示", "演示", "动作", "接触", "物理")):
        return "camera_control"
    if any(term in text for term in ("identity", "face", "wardrobe", "character", "same", "人物", "角色", "服装", "脸", "身份", "女主", "男主")):
        return "character_consistency"
    if any(term in text for term in ("continuity", "across", "result", "carry", "连续", "前后", "结果", "保持")):
        return "narrative_continuity"
    return "instruction_following"


def apply_release_decision(report: EvaluationReport, profile: Dict[str, Any] | None = None) -> EvaluationReport:
    """Apply the rubric's evidence-first release gate in deterministic code.

    The visual model observes frames; it does not authorise publication. A
    weighted score is retained only as a diagnostic for the quality trajectory.
    """
    profile = profile or report.rubric_profile or {}
    floor = float(profile.get("dimension_floor", 3.0))
    overall_floor = float(profile.get("overall_floor", 3.0))
    report.rubric_profile = profile
    report.diagnostic_overall = report.scores.weighted_overall(profile.get("diagnostic_weights"))
    checks = report.atomic_checks or []
    critical = [item for item in checks if item.get("critical")]
    evidenced = [item for item in critical if item.get("status") in {"pass", "fail"} and bool(item.get("evidence"))]
    report.evidence_coverage = {
        "critical_requirements_total": len(critical),
        "critical_requirements_evidenced": len(evidenced),
        "requirements_total": len(checks),
        "requirements_evidenced": sum(1 for item in checks if item.get("status") in {"pass", "fail"} and bool(item.get("evidence"))),
    }
    active_dimensions = [key for key in (profile.get("release_dimensions") or list(DIMENSIONS)) if key in DIMENSIONS]
    report.dimension_evidence = {
        dimension: {
            "requirements_total": len(related := [item for item in checks if item.get("rubric_dimension") == dimension]),
            "requirements_evidenced": sum(1 for item in related if item.get("status") in {"pass", "fail"} and bool(item.get("evidence"))),
            "requirements_passed": sum(1 for item in related if item.get("status") == "pass" and bool(item.get("evidence"))),
            "requirements_failed": sum(1 for item in related if item.get("status") == "fail"),
        }
        for dimension in active_dimensions
    }
    if not critical or len(evidenced) != len(critical):
        report.release_decision = "insufficient_evidence"
        return report
    if any(item.get("status") == "fail" for item in critical) or any(case.severity == Severity.BLOCKING for case in report.bad_cases):
        report.release_decision = "fail"
        return report
    dimensions_ok = bool(active_dimensions) and all(getattr(report.scores, key) >= floor for key in active_dimensions)
    overall_ok = report.diagnostic_overall >= overall_floor
    report.release_decision = "pass" if dimensions_ok and overall_ok else "fail"
    return report


def is_publishable(report: EvaluationReport) -> bool:
    return report.release_decision == "pass"
