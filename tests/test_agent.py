import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.agent.planner import DeepSeekControlPlanner
from backend.app.agent.context import ContextEnrichmentAgent
from backend.app.agent.prompt_compiler import PromptCompiler
from backend.app.agent.video_capabilities import VideoCapabilityRegistry
from backend.app.agent.sampling import fallback_evidence_sampling_policy, validate_evidence_sampling_policy
from backend.app.agent.generation_spec import fallback_generation_spec, validate_generation_spec, validate_brief_duration
from backend.app.agent.duration_planning import build_duration_plan
from backend.app.agent.asset_manifest import bind_criteria_to_assets, build_asset_manifest, evaluation_reference_entries
from backend.app.demo import create_demo_state, create_enriched_project_state, create_orchestrator, create_project_state
from backend.app.tools.evaluator import ArkVisionEvaluator, ResilientVisionEvaluator, VisionObservationUnavailable
from backend.app.tools.generator import ArkVideoGenerator
from backend.app.tools.minimax_hailuo import MiniMaxHailuoGenerator
from backend.app.tools.minimax_h3 import MiniMaxH3Generator
from backend.app.tools.audio_probe import AudioPresenceProbe
from backend.app.tools.repair import rewrite_generation_task
from backend.app.domain.models import EvaluationReport, GenerationJob, RubricScores, VersionReview
from backend.app.domain.rubric import apply_release_decision, dimension_for_criterion


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.state = create_demo_state()
        self.agent = create_orchestrator()

    def test_blocking_action_chain_is_split(self):
        self.agent.run_once(self.state)
        self.assertEqual(self.state.status, "awaiting_repair_upload")
        self.assertEqual(self.state.latest_decision.action.value, "split_shot")
        self.assertEqual(self.state.latest_decision.target_shot, "S2")
        self.assertEqual(self.state.iterations_used, 1)

    def test_repaired_version_is_published(self):
        self.agent.run_once(self.state)
        self.agent.simulate_repaired_upload(self.state)
        self.agent.run_once(self.state)
        self.assertEqual(self.state.status, "published")
        self.assertEqual(self.state.current_version_id, "v2")
        self.assertEqual([report.version_id for report in self.state.evaluation_history], ["v1", "v2"])
        self.assertEqual(self.state.evaluation_history[0].scores.overall(), 3.14)
        self.assertEqual(self.state.evaluation_history[1].scores.overall(), 4.24)
        v1, v2 = self.state.version_reviews
        self.assertEqual(v1.version_id, "v1")
        self.assertIsNotNone(v1.decision)
        self.assertIsNotNone(v1.repair_task)
        self.assertEqual(v2.parent_version_id, "v1")
        self.assertEqual(v2.status, "published")

    def test_evaluation_snapshot_is_not_replaced_when_the_same_version_is_reused(self):
        self.state.current_version_id = "v1"
        self.state.current_video_asset = {"source": "sample", "name": "Prepared sample V1"}
        self.agent.run_once(self.state)
        original_report = self.state.evaluation_history[0]
        self.agent.run_once(self.state)
        self.assertEqual(len(self.state.evaluation_history), 1)
        self.assertIs(self.state.latest_report, original_report)

    def test_human_budget_approval_resumes_reason_act_without_rescoring(self):
        self.state.creative_spec.max_iterations = 1
        self.agent.run_once(self.state)
        frozen_report = self.state.latest_report
        self.assertEqual(self.state.iterations_used, 1)
        self.state.status = "stopped_budget_exhausted"
        self.state.creative_spec.max_iterations = 2

        self.agent.resume_after_human_approval(self.state)

        self.assertEqual(self.state.status, "awaiting_repair_upload")
        self.assertEqual(self.state.iterations_used, 2)
        self.assertEqual(len(self.state.evaluation_history), 1)
        self.assertIs(self.state.latest_report, frozen_report)
        self.assertEqual(self.state.latest_repair_task.target_shot, "S2")

    def test_published_version_can_start_optional_iteration_without_rescoring(self):
        self.agent.run_once(self.state)
        self.agent.simulate_repaired_upload(self.state)
        self.agent.run_once(self.state)
        self.assertEqual(self.state.status, "published")
        frozen_report = self.state.latest_report
        published_review = self.state.version_reviews[-1]

        self.agent.prepare_optional_iteration(self.state)

        self.assertEqual(self.state.status, "awaiting_repair_upload")
        self.assertEqual(self.state.iterations_used, 2)
        self.assertEqual(len(self.state.evaluation_history), 2)
        self.assertIs(self.state.latest_report, frozen_report)
        self.assertEqual(published_review.status, "published")
        self.assertEqual(self.state.latest_decision.priority, "optional")
        self.assertTrue(self.state.latest_repair_task.prompt.startswith("Regenerate"))

    def test_no_repair_upload_without_task(self):
        with self.assertRaises(ValueError):
            self.agent.simulate_repaired_upload(self.state)

    def test_custom_video_cannot_use_simulated_repair(self):
        state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley.")
        state.status = "awaiting_repair_upload"
        state.current_video_asset = {"source": "user_upload", "name": "ebike-v1.mp4"}
        with self.assertRaises(ValueError):
            self.agent.simulate_repaired_upload(state)

    def test_custom_prompt_creates_a_control_plan_with_reference_metadata(self):
        from backend.app.demo import create_demo_state, create_empty_state

        custom = create_demo_state(
            "A red e-bike unfolds in a rainy Tokyo alley, with its logo kept consistent.",
            {"name": "ebike-reference.png", "mime_type": "image/png", "size": "2048"},
        )
        self.assertEqual(custom.creative_spec.goal, "A red e-bike unfolds in a rainy Tokyo alley, with its logo kept consistent.")
        self.assertEqual(custom.creative_spec.reference_asset["name"], "ebike-reference.png")
        self.assertTrue(1 <= len(custom.creative_spec.shots) <= 6)
        self.assertEqual(create_empty_state().status, "ready_for_brief")

    def test_recovery_plan_keeps_the_submitted_brief_visible(self):
        prompt = "A ceramic fox lantern opens in a moonlit garden and reveals a glowing paper star."
        state = create_project_state(prompt)
        self.assertEqual(state.creative_spec.planning_source, "fallback")
        self.assertIn("ceramic fox lantern", state.creative_spec.shots[0]["intent"])
        self.assertIn("submitted brief", state.creative_spec.planning_notice)

    def test_context_agent_requests_market_only_for_high_impact_seasonal_brief(self):
        agent = ContextEnrichmentAgent()
        missing_market = agent.enrich("Create a current seasonal skincare campaign.")
        self.assertEqual(missing_market["status"], "needs_user_confirmation")
        self.assertEqual(missing_market["open_questions"][0]["id"], "target_market")

        singapore = agent.enrich("Create a current seasonal skincare campaign.", target_market="Singapore")
        self.assertEqual(singapore["status"], "ready_for_compile")
        calendar = next(item for item in singapore["tool_observations"] if item["tool"] == "calendar_context")
        self.assertEqual(calendar["result"]["region"], "Singapore")
        self.assertIn("tropical", calendar["result"]["season"])

    def test_enriched_state_uses_existing_planning_path_after_confirmation(self):
        state = create_enriched_project_state("Make a current seasonal travel video.", context_agent=ContextEnrichmentAgent())
        self.assertEqual(state.status, "awaiting_context_confirmation")
        completed = create_enriched_project_state(
            "Make a current seasonal travel video.", context_agent=ContextEnrichmentAgent(), target_market="Australia"
        )
        self.assertEqual(completed.status, "planning_ready")
        self.assertEqual(completed.creative_spec.enriched_context["status"], "ready_for_compile")

    def test_live_control_plan_is_validated_and_preserves_source_prompt(self):
        planner = DeepSeekControlPlanner(api_key="test-key")
        planner._request_json = lambda *_: {
            "objective": "Show a red e-bike unfolding across a rainy Tokyo alley sequence.",
            "immutable_constraints": ["red e-bike shape", "visible logo", "rainy Tokyo alley"],
            "shots": [
                {"shot_id": "S1", "intent": "Establish the e-bike in the rainy alley.", "duration_seconds": 1.0, "critical_moment": "The red e-bike and rainy Tokyo alley are both visible."},
                {"shot_id": "S2", "intent": "Show one complete unfolding action.", "duration_seconds": 3.0, "critical_moment": "The unfolding action reaches its completed state."},
                {"shot_id": "S3", "intent": "Hold on the unfolded e-bike and logo.", "duration_seconds": 1.0, "critical_moment": "The unfolded geometry and logo remain readable."},
            ],
            "risk_forecast": ["product geometry drift", "logo inconsistency"],
            "evidence_sampling": {
                "focus_shot_ids": ["S2", "S3"],
                "critical_moments": [
                    {"shot_id": "S2", "reason": "Verify the e-bike completes the unfolding action."},
                    {"shot_id": "S3", "reason": "Verify the unfolded product shape and logo remain visible."},
                ],
                "rationale": "Focus extra visual evidence on the action and its final product state.",
            },
            "generation_spec": {
                "duration_seconds": 5,
                "aspect_ratio": "16:9",
                "quality_target": "high",
                "rationale": "A product action needs a concise landscape showcase with clear logo detail.",
            },
            "production_direction": {
                "camera_language": "Begin wide enough to identify the alley, then hold a stable product-readable framing during the unfolding action.",
                "visual_treatment": "Use rain-reflective neon lighting with clean product edges and a readable logo plane.",
                "motion_continuity": "Keep the e-bike centred and preserve its folded-to-unfolded geometry without a state jump.",
                "negative_guards": ["Do not introduce other products or logos.", "Do not obscure the unfolding mechanism with rain or camera shake."],
            },
            "acceptance_criteria": [
                {"requirement_id": "s1_product", "shot_id": "S1", "source": "prompt", "requirement": "The red e-bike is visible in the rainy Tokyo alley.", "expected": "The product and setting are both visible.", "critical": True},
                {"requirement_id": "s2_action", "shot_id": "S2", "source": "control_plan", "requirement": "The e-bike completes one unfolding action.", "expected": "The e-bike is visibly unfolded.", "critical": True},
                {"requirement_id": "s3_logo", "shot_id": "S3", "source": "prompt", "requirement": "The unfolded e-bike logo remains visible.", "expected": "A consistent logo is visible on the unfolded product.", "critical": True},
            ],
            "recommended_iterations": 1,
            "planning_rationale": "One atomic action has a limited but visible product-consistency risk.",
        }
        prompt = "A red e-bike unfolds in a rainy Tokyo alley. Keep the logo consistent."
        state = create_project_state(prompt, control_planner=planner)
        self.assertEqual(state.creative_spec.planning_source, "deepseek")
        self.assertEqual(state.creative_spec.source_prompt, prompt)
        self.assertEqual(state.creative_spec.goal, "Show a red e-bike unfolding across a rainy Tokyo alley sequence.")
        self.assertEqual(state.creative_spec.recommended_iterations, 1)
        # The live planner's action-focused policy is intentionally refined by
        # the product rubric: product integrity must be compared from first
        # appearance to final display when a logo is a key requirement.
        self.assertEqual(state.creative_spec.evidence_sampling["focus_shot_ids"], ["S1", "S3"])
        self.assertEqual(state.creative_spec.rubric_profile["profile_id"], "product_ad")
        # The model provides semantic beat pacing; deterministic editorial
        # policy derives 2s establish + 6s transformation + 2s payoff.
        self.assertEqual(state.creative_spec.generation_spec["duration_seconds"], 10)
        self.assertEqual(state.creative_spec.generation_spec["quality_target"], "high")
        self.assertEqual([(shot["start_seconds"], shot["end_seconds"]) for shot in state.creative_spec.shots], [(0.0, 2.0), (2.0, 8.0), (8.0, 10.0)])
        self.assertEqual(state.creative_spec.evidence_sampling["planned_duration_seconds"], 10.0)
        self.assertEqual(state.creative_spec.production_direction["source"], "deepseek")
        compiled_prompt = ArkVideoGenerator._generation_prompt(state, None, include_output_hints=False)
        self.assertIn("Agent camera direction: Begin wide enough", compiled_prompt)
        self.assertIn("Do not obscure the unfolding mechanism", compiled_prompt)

    def test_live_planner_can_choose_one_or_four_semantic_beats(self):
        """Storyboard count is a planning decision, not a UI or evaluator template."""
        planner = DeepSeekControlPlanner(api_key="test-key")

        def plan_for(shots):
            shot_ids = [shot["shot_id"] for shot in shots]
            return {
                "objective": "A concise, controllable video plan.",
                "immutable_constraints": ["subject identity", "setting", "visual style"],
                "shots": shots,
                "risk_forecast": ["subject drift", "key action may be incomplete"],
                "evidence_sampling": {
                    "focus_shot_ids": [shot_ids[-1]],
                    "critical_moments": [{"shot_id": shot_ids[-1], "reason": "Verify the intended final visible state."}],
                    "rationale": "Prioritise the most consequential visible beat.",
                },
                "generation_spec": {"duration_seconds": 8, "aspect_ratio": "16:9", "quality_target": "standard", "rationale": "A short controlled clip."},
                "production_strategy": {"mode": "single_clip", "reason": "These semantic beats fit one continuous clip."},
                "acceptance_criteria": [
                    {"requirement_id": f"{shot_id.lower()}_visible", "shot_id": shot_id, "source": "control_plan", "requirement": "The planned beat is visibly completed.", "expected": "Evidence supports the planned state.", "critical": True}
                    for shot_id in shot_ids
                ],
                "recommended_iterations": 1,
                "planning_rationale": "The repair budget follows the visible controllability risk.",
            }

        one_beat = [{"shot_id": "S1", "intent": "Hold on one readable product detail.", "beat_function": "detail", "pacing": "readable", "timing_reason": "One static detail only needs a single observable beat.", "critical_moment": "The product detail remains readable."}]
        planner._request_json = lambda *_: plan_for(one_beat)
        one_state = create_project_state("Show one perfume bottle on a plinth.", control_planner=planner)
        self.assertEqual([shot["shot_id"] for shot in one_state.creative_spec.shots], ["S1"])
        self.assertEqual(one_state.creative_spec.evidence_sampling["focus_shot_ids"], ["S1"])

        four_beats = [
            {"shot_id": "S1", "intent": "Establish the runner and bridge.", "beat_function": "establish", "pacing": "readable", "timing_reason": "The location must be legible before motion begins.", "critical_moment": "Runner and bridge are visible."},
            {"shot_id": "S2", "intent": "Show the runner starting the jump.", "beat_function": "action", "pacing": "dynamic", "timing_reason": "The take-off needs readable motion.", "critical_moment": "The jump begins clearly."},
            {"shot_id": "S3", "intent": "Carry the runner through the jump arc.", "beat_function": "transition", "pacing": "bridge", "timing_reason": "The motion state must remain continuous.", "critical_moment": "The mid-air transition is visible."},
            {"shot_id": "S4", "intent": "Hold on the landing outcome.", "beat_function": "payoff", "pacing": "hold", "timing_reason": "The final landing needs verification time.", "critical_moment": "The safe landing is visible."},
        ]
        planner._request_json = lambda *_: plan_for(four_beats)
        four_state = create_project_state("A runner crosses a bridge and lands a jump.", control_planner=planner)
        self.assertEqual([shot["shot_id"] for shot in four_state.creative_spec.shots], ["S1", "S2", "S3", "S4"])
        self.assertEqual(four_state.creative_spec.evidence_sampling["planned_duration_seconds"], 10.0)

    def test_live_planner_can_compile_a_six_beat_fifteen_second_commercial(self):
        """A concise commercial brief can need more than a setup/action/payoff trio."""
        planner = DeepSeekControlPlanner(api_key="test-key")
        shots = [
            ("S1", "establish", "readable", "Open on a heat-hazed summer table; frame the unopened ice-cream pack as the visual problem.", "The product pack and hot visual context are readable."),
            ("S2", "detail", "readable", "Move into the pack opening and reveal the ice cream texture without obscuring the product.", "The product reveal and wrapper state are visible."),
            ("S3", "action", "dynamic", "Follow one clean bite from hand to mouth, keeping the ice cream and reaction in one readable action.", "The bite begins and completes with the product still visible."),
            ("S4", "detail", "hold", "Hold a macro detail of the chilled surface and melting edge with controlled highlights.", "The cold texture and melt detail remain readable."),
            ("S5", "payoff", "hold", "Settle on the refreshed end state, using a gentle push-in rather than a new unplanned event.", "The emotional payoff is visible and continuous with the preceding action."),
            ("S6", "detail", "readable", "Finish on a clean product hero composition with space reserved for a user-supplied brand treatment.", "The final product composition is stable and readable."),
        ]
        planner._request_json = lambda *_: {
            "objective": "Create a premium, refreshing ice-cream commercial with a heat-to-cool sensory arc.",
            "immutable_constraints": ["ice-cream commercial", "creative visual tone", "product remains visually readable"],
            "shots": [
                {"shot_id": shot_id, "intent": intent, "beat_function": function, "pacing": pacing, "timing_reason": "This visual beat needs its allocated readable screen time.", "critical_moment": moment}
                for shot_id, function, pacing, intent, moment in shots
            ],
            "risk_forecast": ["product shape drift", "bite action may become unreadable", "detail texture can turn artificial"],
            "evidence_sampling": {"focus_shot_ids": ["S2", "S3", "S6"], "critical_moments": [{"shot_id": "S2", "reason": "Verify product reveal."}, {"shot_id": "S3", "reason": "Verify bite completion."}, {"shot_id": "S6", "reason": "Verify final product hero."}], "rationale": "Sample product reveal, sensory action, and final product state."},
            "generation_spec": {"duration_seconds": 15, "aspect_ratio": "9:16", "quality_target": "high", "rationale": "A short vertical commercial needs a complete sensory arc and readable product finish."},
            "production_strategy": {"mode": "single_clip", "reason": "The six micro-beats form one continuous commercial arc within fifteen seconds."},
            "production_direction": {"camera_language": "Use controlled product macro shots, one readable bite action, and a stable final hero composition.", "visual_treatment": "Transition from warm, heat-hazed highlights to clean cool tones and crisp dairy texture.", "motion_continuity": "Keep the product and hand state continuous from reveal through the bite to the final hero frame.", "sound_design": "Use a subtle wrapper tear, bite texture cue, and a clean refreshing resolve; no invented lyrics.", "negative_guards": ["Do not invent a brand name or slogan.", "Do not obscure the product during the key bite."]},
            "acceptance_criteria": [{"requirement_id": f"{shot_id.lower()}_visible", "shot_id": shot_id, "source": "control_plan", "requirement": "The planned commercial beat is visibly readable.", "expected": moment, "critical": True} for shot_id, _, _, _, moment in shots],
            "recommended_iterations": 2,
            "planning_rationale": "Two repair rounds cover product readability, action completion, and texture consistency without an open-ended generation loop.",
        }
        preview_spec = planner.plan("制作一条15秒雪糕商业广告视频，要有创意感")
        self.assertEqual(len(preview_spec.shots), 6)
        state = create_project_state("制作一条15秒雪糕商业广告视频，要有创意感", control_planner=planner)
        self.assertEqual([shot["shot_id"] for shot in state.creative_spec.shots], ["S1", "S2", "S3", "S4", "S5", "S6"])
        self.assertEqual(state.creative_spec.generation_spec["duration_seconds"], 15)
        self.assertIn("wrapper tear", state.creative_spec.production_direction["sound_design"])

    def test_live_planning_fallback_is_visible_in_the_audit_trail(self):
        planner = DeepSeekControlPlanner(api_key="test-key")

        def unavailable(*_args):
            raise OSError("simulated network failure")

        planner._request_json = unavailable
        state = create_project_state("Show one static product detail.", control_planner=planner)
        self.assertEqual(state.creative_spec.planning_source, "fallback")
        self.assertIn("DeepSeek did not return", state.creative_spec.planning_notice)
        self.assertTrue(any(item.get("event") == "control_planner_fallback" for item in state.history))

    def test_sampling_policy_is_bound_to_planned_shots(self):
        policy = validate_evidence_sampling_policy({
            "focus_shot_ids": ["S2", "S3"],
            "critical_moments": [{"shot_id": "S2", "reason": "Verify the complete primary action."}],
            "rationale": "Capture the key action and final continuity state.",
        })
        self.assertEqual(policy["focus_shot_ids"], ["S2", "S3"])
        fallback = fallback_evidence_sampling_policy(
            [{"shot_id": "S1", "intent": "Open."}, {"shot_id": "S2", "intent": "Act."}, {"shot_id": "S3", "intent": "Resolve."}],
            ["Cross-shot continuity"],
        )
        self.assertTrue(set(fallback["focus_shot_ids"]).issubset({"S1", "S2", "S3"}))

    def test_generation_spec_is_prompt_inferred_and_constrained(self):
        spec = fallback_generation_spec("A vertical three-shot product story for TikTok with close-up logo detail.")
        self.assertEqual(spec["aspect_ratio"], "9:16")
        self.assertEqual(spec["duration_seconds"], 10)
        self.assertEqual(spec["quality_target"], "high")
        self.assertEqual(validate_generation_spec(spec), spec)

    def test_generation_spec_prioritises_explicit_user_duration(self):
        spec = fallback_generation_spec("Create a 12-second vertical product film with a clear reveal.")
        self.assertEqual(spec["duration_seconds"], 12)
        self.assertIn("explicitly requested", spec["rationale"])
        self.assertEqual(validate_generation_spec(spec)["duration_seconds"], 12)

    def test_generation_spec_fallback_uses_brief_complexity_not_fixed_eight_seconds(self):
        self.assertEqual(fallback_generation_spec("A clean product logo reveal.")["duration_seconds"], 6)
        self.assertEqual(fallback_generation_spec("A dancer spins, jumps, and lands cleanly.")["duration_seconds"], 8)
        self.assertEqual(fallback_generation_spec("First show the shop, then reveal the product, and finally show the customer reaction.")["duration_seconds"], 12)

    def test_editorial_duration_plan_uses_beat_pacing_and_preserves_explicit_duration(self):
        beats = [
            {"shot_id": "S1", "intent": "Establish the product and setting.", "beat_function": "establish", "pacing": "readable", "critical_moment": "Product and setting are visible."},
            {"shot_id": "S2", "intent": "Show the product unfold completely.", "beat_function": "action", "pacing": "transformation", "critical_moment": "The product reaches its unfolded state."},
            {"shot_id": "S3", "intent": "Hold on the final product state.", "beat_function": "payoff", "pacing": "hold", "critical_moment": "The final product state remains readable."},
        ]
        inferred = build_duration_plan(beats)
        self.assertEqual(inferred["total_duration_seconds"], 10)
        self.assertEqual([shot["duration_seconds"] for shot in inferred["timeline"]], [2.0, 6.0, 2.0])

        explicit = build_duration_plan(beats, explicit_duration_seconds=12)
        self.assertEqual(explicit["source"], "user_specified")
        self.assertEqual(explicit["total_duration_seconds"], 12)
        self.assertEqual(sum(shot["duration_seconds"] for shot in explicit["timeline"]), 12.0)

    def test_editorial_duration_plan_blocks_a_segmented_story_from_single_clip_generation(self):
        beats = [
            {"shot_id": "S1", "intent": "Establish one city.", "critical_moment": "City A is visible."},
            {"shot_id": "S2", "intent": "Travel through several decades.", "critical_moment": "The time transitions are visible."},
            {"shot_id": "S3", "intent": "Resolve in a different world.", "critical_moment": "The final world is visible."},
        ]
        plan = build_duration_plan(beats, strategy={"mode": "needs_segmentation", "reason": "The story needs independent locations and time jumps."})
        self.assertEqual(plan["mode"], "needs_segmentation")
        self.assertEqual(plan["timeline"], [])

    def test_generation_spec_rejects_minute_duration_over_limit(self):
        with self.assertRaisesRegex(ValueError, "15"):
            fallback_generation_spec("制作一条 1 分钟的品牌故事视频，展示三个阶段。")

    def test_generation_spec_rejects_duration_over_fifteen_seconds(self):
        with self.assertRaisesRegex(ValueError, "15"):
            validate_brief_duration("请生成一支 16 秒的广告视频")
        with self.assertRaisesRegex(ValueError, "15"):
            fallback_generation_spec("Create a 20-second product story")
        with self.assertRaisesRegex(ValueError, "15"):
            validate_generation_spec({"duration_seconds": 16, "aspect_ratio": "16:9", "quality_target": "standard", "rationale": "too long"})
        with self.assertRaisesRegex(ValueError, "15"):
            validate_brief_duration("开场 10 秒，但总时长为 20 秒")

    def test_generation_tool_selects_reference_strategy_only_when_pixels_exist(self):
        generator = ArkVideoGenerator(api_key="test-key", text_model="seedance-text", reference_model="seedance-reference")
        text_state = create_demo_state("A red e-bike unfolds in a rainy alley.")
        self.assertEqual(generator.select_tool(text_state)["tool_name"], "generate_text_video")
        reference_state = create_demo_state(
            "A red e-bike unfolds in a rainy alley.",
            {"name": "bike.png", "preview_data_url": "data:image/jpeg;base64,abc"},
        )
        selected = generator.select_tool(reference_state)
        self.assertEqual(selected["tool_name"], "generate_reference_video")
        self.assertEqual(selected["model_id"], "seedance-reference")

    def test_legacy_seedance_test_adapter_never_assumes_reference_image_support(self):
        generator = ArkVideoGenerator(api_key="test-key", text_model="doubao-seedance-1-0-pro")
        reference_state = create_demo_state(
            "A red e-bike unfolds in a rainy alley.",
            {"name": "bike.png", "preview_data_url": "data:image/jpeg;base64,abc"},
        )
        selected = generator.select_tool(reference_state)
        self.assertEqual(selected["tool_name"], "generate_text_video")
        self.assertEqual(selected["request_mode"], "text_to_video")
        self.assertEqual(selected["capability_profile_id"], "seedance_1_0_pro_test")
        self.assertIn("not declared reference-conditioned", selected["reason"])

    def test_context_router_and_compiler_share_capability_declaration(self):
        reference = {"name": "bike.png", "preview_data_url": "data:image/jpeg;base64,abc"}
        registry = VideoCapabilityRegistry(text_model="seedance-text", reference_model="seedance-reference")
        context = ContextEnrichmentAgent(text_video_model="seedance-text", reference_video_model="seedance-reference")
        routed = context._capability_router(reference)
        self.assertEqual(routed["request_mode"], "image_conditioned_video")
        state = create_demo_state("A red e-bike unfolds in a rainy alley.", reference)
        PromptCompiler(capability_registry=registry).compile(state)
        binding = state.creative_spec.generation_contract["provider_binding"]
        self.assertEqual(binding["intended_mode"], "image_conditioned_video")
        self.assertEqual(binding["capability_profile"]["profile_id"], routed["capability_profile_id"])
        self.assertEqual(state.creative_spec.generation_contract["reference_strategy"]["mode"], "reference_conditioned")

    def test_minimax_hailuo_routes_first_ordered_image_to_documented_i2v(self):
        reference = {"name": "bike.png", "preview_data_url": "data:image/jpeg;base64,abc"}
        registry = VideoCapabilityRegistry(
            provider="minimax_hailuo",
            minimax_api_key="test-key",
            minimax_model="MiniMax-Hailuo-2.3",
        )
        selected = registry.route(reference)
        self.assertEqual(selected["provider_id"], "minimax_hailuo_adapter")
        self.assertEqual(selected["request_mode"], "image_to_video")
        self.assertEqual(selected["capability_profile_id"], "minimax_hailuo_2_3_official")
        self.assertEqual(selected["tool_name"], "generate_first_frame_video")
        self.assertIn("first_frame_image", selected["reason"])

    def test_minimax_h3_routes_multimodal_assets_to_documented_reference_mode(self):
        image = {"name": "character.png", "media_type": "image", "preview_data_url": "data:image/jpeg;base64,character"}
        audio = {"name": "beat.wav", "media_type": "audio", "data_url": "data:audio/wav;base64,audio"}
        registry = VideoCapabilityRegistry(provider="minimax_h3", minimax_api_key="test-key", minimax_model="MiniMax-H3")
        selected = registry.route(image, [image, audio])
        self.assertEqual(selected["provider_id"], "minimax_h3_adapter")
        self.assertEqual(selected["request_mode"], "reference_to_video")
        self.assertEqual(selected["capability_profile_id"], "minimax_h3_v2_official")
        self.assertIn("reference_image", selected["reason"])
        self.assertIn("reference_audio", selected["reason"])

    def test_minimax_h3_preserves_ordered_image_and_audio_references_in_content(self):
        image = {"name": "character.png", "media_type": "image", "preview_data_url": "data:image/jpeg;base64,character", "prompt_mentions": ["@character.png"]}
        audio = {"name": "beat.mp3", "media_type": "audio", "data_url": "data:audio/mpeg;base64,audio", "prompt_mentions": ["@beat.mp3"]}
        state = create_demo_state("Let @character.png move to @beat.wav.", image, multimodal_assets=[image, audio])
        state.creative_spec.generation_spec["duration_seconds"] = 8
        state.creative_spec.generation_contract["output_spec"]["duration_seconds"] = 8
        generator = MiniMaxH3Generator(api_key="test-key", resolution="768P")
        body = generator._request_body(state, generator.select_tool(state), "Compiled instruction.")
        self.assertEqual(body["model"], "MiniMax-H3")
        self.assertEqual(body["duration"], 8)
        self.assertEqual(body["ratio"], state.creative_spec.generation_contract["output_spec"]["aspect_ratio"])
        self.assertEqual([item.get("role") for item in body["content"][1:]], ["reference_image", "reference_audio"])
        self.assertEqual(body["content"][1]["image_url"]["url"], image["preview_data_url"])
        self.assertEqual(body["content"][2]["audio_url"]["url"], "data:audio/mp3;base64,audio")

    def test_minimax_h3_normalises_browser_wav_alias(self):
        self.assertEqual(
            MiniMaxH3Generator._normalise_audio_data_url("data:audio/x-wav;base64,audio"),
            "data:audio/wav;base64,audio",
        )

    def test_vision_score_normaliser_accepts_unambiguous_model_formats_only(self):
        normalise = ArkVisionEvaluator._normalise_score
        self.assertEqual(normalise(4), 4.0)
        self.assertEqual(normalise("4/5"), 4.0)
        self.assertEqual(normalise("3.5 分"), 3.5)
        self.assertEqual(normalise({"score": "2.0"}), 2.0)
        self.assertIsNone(normalise("3-4"))
        self.assertIsNone(normalise("N/A"))
        self.assertIsNone(normalise(None))

    def test_sparse_keyframe_evidence_cannot_present_as_perfect_quality(self):
        values, notes = ArkVisionEvaluator._calibrate_scores_with_evidence(
            {
                "instruction_following": 5.0,
                "character_consistency": 5.0,
                "camera_control": 5.0,
                "visual_quality": 5.0,
                "narrative_continuity": 5.0,
                "audio_visual_alignment": 0.0,
            },
            [
                {
                    "rubric_dimension": "instruction_following",
                    "status": "pass",
                    "evidence": [{"frame_id": "F1"}],
                },
                {
                    "rubric_dimension": "visual_quality",
                    "status": "pass",
                    "evidence": [{"frame_id": "F1"}],
                },
            ],
            {"release_dimensions": ["instruction_following", "visual_quality"]},
        )
        self.assertEqual(values["instruction_following"], 4.0)
        self.assertEqual(values["visual_quality"], 4.0)
        self.assertEqual(values["character_consistency"], 5.0)  # inactive and hidden from the release view
        self.assertEqual(len(notes), 2)

        values, _ = ArkVisionEvaluator._calibrate_scores_with_evidence(
            values,
            [{"rubric_dimension": "visual_quality", "status": "fail", "evidence": [{"frame_id": "F1"}]}],
            {"release_dimensions": ["visual_quality"]},
        )
        self.assertEqual(values["visual_quality"], 2.0)

    def test_vision_evaluator_retries_once_when_the_first_response_breaks_score_schema(self):
        class SchemaRetryEvaluator(ArkVisionEvaluator):
            def __init__(self):
                super().__init__(api_key="test-key", model="test-model")
                self.calls = []

            def _evaluate_content(self, state, content, frame_shot_map):
                self.calls.append(content)
                if len(self.calls) == 1:
                    raise ValueError("Invalid vision score for camera_control: 'N/A'")
                return EvaluationReport(
                    version_id="v1",
                    scores=RubricScores(4.0, 4.0, 4.0, 4.0, 4.0, 0.0),
                    bad_cases=[],
                    summary="The same retained evidence was evaluated with a valid score schema.",
                    observation_source="doubao_seed_2_lite",
                    observation_notice="Initial evaluation completed.",
                )

        state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley.")
        state.current_version_id = "v1"
        state.current_video_asset = {"source": "agent_generated", "name": "v1.mp4"}
        with TemporaryDirectory() as directory:
            frame = Path(directory) / "f1.jpg"
            frame.write_bytes(b"not-a-real-jpeg-needed-by-this-stub")
            state.evaluation_frame_records = [{
                "frame_id": "F1", "timestamp": 1.0, "path": str(frame),
                "expected_shot_id": "S1", "sampling_reason": "Opening evidence.",
            }]
            report = SchemaRetryEvaluator().analyze(state)

        self.assertEqual(report.scores.camera_control, 4.0)
        self.assertIn("schema retry", report.observation_notice.lower())

    def test_minimax_h3_refresh_uses_v2_task_path_and_reads_succeeded_status(self):
        class Response:
            def read(self):
                return b'{"task":{"status":"succeeded","content":{"url":"https://example.test/video.mp4"}}}'

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        job = GenerationJob("task/a?b", "v1", "generate", "MiniMax-H3", "prompt")
        generator = MiniMaxH3Generator(api_key="test-key", base_url="https://api.minimaxi.com")
        with patch("backend.app.tools.minimax_h3.urllib.request.urlopen", return_value=Response()) as open_mock:
            refreshed = generator.refresh(job)

        request = open_mock.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.minimaxi.com/v2/query/video_generation/task%2Fa%3Fb")
        self.assertEqual(refreshed.status, "succeeded")
        self.assertEqual(refreshed.output_video_url, "https://example.test/video.mp4")

    def test_minimax_h3_refresh_surfaces_cancelled_task_instead_of_running_forever(self):
        class Response:
            def read(self):
                return b'{"task":{"status":"cancelled"}}'

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        job = GenerationJob("task-123", "v1", "generate", "MiniMax-H3", "prompt")
        generator = MiniMaxH3Generator(api_key="test-key")
        with patch("backend.app.tools.minimax_h3.urllib.request.urlopen", return_value=Response()):
            refreshed = generator.refresh(job)

        self.assertEqual(refreshed.status, "failed")
        self.assertIn("cancelled", refreshed.error.lower())

    def test_asset_manifest_preserves_order_and_only_exposes_explicit_images_to_visual_qa(self):
        character = {
            "name": "character.png", "media_type": "image", "input_order": 2,
            "preview_data_url": "data:image/png;base64,character", "prompt_mentions": ["@character.png"],
        }
        mood = {
            "name": "mood.png", "media_type": "image", "input_order": 1,
            "preview_data_url": "data:image/png;base64,mood", "prompt_mentions": [],
        }
        music = {
            "name": "beat.wav", "media_type": "audio", "input_order": 3,
            "data_url": "data:audio/wav;base64,beat", "prompt_mentions": ["@beat.wav"],
        }
        manifest = build_asset_manifest([character, mood, music])
        self.assertEqual([item["name"] for item in manifest], ["mood.png", "character.png", "beat.wav"])
        self.assertEqual([item["asset_id"] for item in evaluation_reference_entries(manifest)], ["R2"])

    def test_compiler_binds_only_named_reference_criteria_and_shares_manifest(self):
        character = {
            "name": "character.png", "media_type": "image",
            "preview_data_url": "data:image/png;base64,character", "prompt_mentions": ["@character.png"],
        }
        state = create_demo_state("Show @character.png turning toward camera.", character, multimodal_assets=[character])
        state.creative_spec.acceptance_criteria[0]["requirement"] += " Match @character.png in the visible opening state."
        PromptCompiler().compile(state)
        generation = state.creative_spec.generation_contract
        evaluation = state.creative_spec.evaluation_contract
        self.assertEqual(generation["asset_manifest"], evaluation["asset_manifest"])
        self.assertEqual(evaluation["acceptance_criteria"][0]["asset_bindings"], ["R1"])
        unbound = bind_criteria_to_assets(
            [{"requirement": "Keep the subject centered.", "expected": "Centered."}], generation["asset_manifest"],
        )
        self.assertEqual(unbound[0]["asset_bindings"], [])
    def test_minimax_hailuo_request_uses_documented_text_to_video_fields(self):
        state = create_demo_state("A vertical three-shot product story for TikTok.")
        state.creative_spec.generation_spec["duration_seconds"] = 10
        state.creative_spec.generation_contract["output_spec"]["duration_seconds"] = 10
        state.creative_spec.shots = [
            {**shot, "duration_seconds": 10 / 3, "start_seconds": index * 10 / 3, "end_seconds": (index + 1) * 10 / 3}
            for index, shot in enumerate(state.creative_spec.shots)
        ]
        generator = MiniMaxHailuoGenerator(api_key="test-key", resolution="768P")
        selection = generator.select_tool(state)
        prompt = ArkVideoGenerator._generation_prompt(state, None, include_output_hints=False)
        body = generator._request_body(state, selection, prompt)
        self.assertEqual(body["model"], "MiniMax-Hailuo-2.3")
        self.assertEqual(body["resolution"], "768P")
        self.assertEqual(body["duration"], 10)
        self.assertIn("prompt", body)
        self.assertNotIn("content", body)
        self.assertNotIn("ratio", body)
        self.assertNotIn("first_frame_image", body)
        self.assertNotIn("--ratio", body["prompt"])

    def test_minimax_hailuo_uses_first_image_and_retains_other_multimodal_assets(self):
        images = [
            {"name": "character.png", "media_type": "image", "preview_data_url": "data:image/jpeg;base64,character"},
            {"name": "product.png", "media_type": "image", "preview_data_url": "data:image/jpeg;base64,product"},
        ]
        audio = {"name": "music.wav", "media_type": "audio", "data_url": "data:audio/wav;base64,audio"}
        state = create_demo_state(
            "A character introduces a product to the beat of the supplied music.",
            images[0],
            multimodal_assets=[*images, audio],
        )
        generator = MiniMaxHailuoGenerator(api_key="test-key", resolution="768P")
        selection = generator.select_tool(state)

        self.assertEqual(selection["request_mode"], "image_to_video")
        self.assertEqual(selection["applied_reference_name"], "character.png")
        self.assertIn("remaining 2 reference asset", selection["reason"])
        self.assertIn("Additional images and all audio remain", selection["capability_profile"]["verification_notice"])

    def test_minimax_hailuo_keeps_attachment_order_in_project_memory(self):
        character = {
            "name": "character.png", "media_type": "image",
            "preview_data_url": "data:image/jpeg;base64,character", "prompt_mentions": ["@character.png"],
        }
        music = {
            "name": "beat.wav", "media_type": "audio",
            "data_url": "data:audio/wav;base64,beat", "prompt_mentions": ["@beat.wav"],
        }
        product = {
            "name": "product.png", "media_type": "image",
            "preview_data_url": "data:image/jpeg;base64,product", "prompt_mentions": [],
        }
        source = "Let @character.png present the product to @beat.wav."
        state = create_demo_state(source, character, multimodal_assets=[character, music, product])
        generator = MiniMaxHailuoGenerator(api_key="test-key", resolution="768P")
        selection = generator.select_tool(state)

        self.assertEqual([asset["name"] for asset in state.creative_spec.multimodal_assets], ["character.png", "beat.wav", "product.png"])
        self.assertEqual(state.creative_spec.multimodal_assets[0]["prompt_mentions"], ["@character.png"])
        self.assertEqual(state.creative_spec.multimodal_assets[1]["prompt_mentions"], ["@beat.wav"])
        self.assertEqual(selection["request_mode"], "image_to_video")
        self.assertEqual(selection["applied_reference_name"], "character.png")

    def test_minimax_hailuo_uses_mentioned_first_image_as_documented_first_frame(self):
        reference = {
            "name": "hero.jpg",
            "media_type": "image",
            "preview_data_url": "data:image/jpeg;base64,hero",
            "prompt_mentions": ["@hero.jpg"],
        }
        state = create_demo_state("Use @hero.jpg as the lead character.", reference, multimodal_assets=[reference])
        generator = MiniMaxHailuoGenerator(api_key="test-key", resolution="768P")
        selection = generator.select_tool(state)
        state.creative_spec.generation_spec["duration_seconds"] = 6
        state.creative_spec.generation_contract["output_spec"]["duration_seconds"] = 6
        self.assertEqual(selection["request_mode"], "image_to_video")
        body = generator._request_body(state, selection, "Compiled instruction.")
        self.assertEqual(body["first_frame_image"], reference["preview_data_url"])
        self.assertEqual(state.creative_spec.multimodal_assets[0]["prompt_mentions"], ["@hero.jpg"])

    def test_context_router_retains_audio_without_claiming_hailuo_audio_conditioning(self):
        audio = {"name": "music.wav", "media_type": "audio", "data_url": "data:audio/wav;base64,audio"}
        agent = ContextEnrichmentAgent()
        agent.capabilities = VideoCapabilityRegistry(provider="minimax_hailuo", minimax_api_key="test-key")
        selected = agent._capability_router(None, [audio])
        self.assertEqual(selected["request_mode"], "text_to_video")
        self.assertIn("audio", selected["reason"])

    def test_minimax_hailuo_uses_first_image_and_never_submits_audio_as_conditioning(self):
        reference = {"name": "bike.png", "media_type": "image", "preview_data_url": "data:image/jpeg;base64,abc"}
        audio = {"name": "beat.wav", "media_type": "audio", "data_url": "data:audio/wav;base64,audio"}
        registry = VideoCapabilityRegistry(
            provider="minimax_hailuo", minimax_api_key="test-key", minimax_model="MiniMax-Hailuo-2.3",
        )
        selected = registry.route(reference, [reference, audio])
        self.assertEqual(selected["provider_id"], "minimax_hailuo_adapter")
        self.assertEqual(selected["request_mode"], "image_to_video")
        self.assertEqual(selected["capability_profile_id"], "minimax_hailuo_2_3_official")
        self.assertIn("audio is retained", selected["reason"])

    def test_minimax_hailuo_i2v_request_uses_only_first_ordered_image(self):
        character = {
            "name": "character.png", "media_type": "image",
            "preview_data_url": "data:image/jpeg;base64,character", "prompt_mentions": ["@character.png"],
        }
        audio = {
            "name": "beat.wav", "media_type": "audio",
            "data_url": "data:audio/wav;base64,audio", "prompt_mentions": ["@beat.wav"],
        }
        state = create_demo_state("Let @character.png move to @beat.wav.", character, multimodal_assets=[character, audio])
        state.creative_spec.generation_spec["duration_seconds"] = 6
        state.creative_spec.generation_contract["output_spec"]["duration_seconds"] = 6
        generator = MiniMaxHailuoGenerator(api_key="test-key", resolution="768P")
        body = generator._request_body(state, generator.select_tool(state), "Compiled instruction.")
        self.assertEqual(body["model"], "MiniMax-Hailuo-2.3")
        self.assertEqual(body["duration"], 6)
        self.assertEqual(body["first_frame_image"], character["preview_data_url"])
        self.assertNotIn("reference_audio", body)
        self.assertNotIn("content", body)

    def test_prompt_compiler_records_hailuo_first_frame_boundary(self):
        reference = {"name": "hero.png", "media_type": "image", "preview_data_url": "data:image/png;base64,hero"}
        audio = {"name": "beat.wav", "media_type": "audio", "data_url": "data:audio/wav;base64,beat"}
        state = create_demo_state("Use @hero.png in a product film.", reference, multimodal_assets=[reference, audio])
        compiler = PromptCompiler(capability_registry=VideoCapabilityRegistry(
            provider="minimax_hailuo", minimax_api_key="test-key", minimax_model="MiniMax-Hailuo-2.3",
        ))
        compiler.compile(state)
        strategy = state.creative_spec.generation_contract["reference_strategy"]
        self.assertEqual(strategy["mode"], "first_frame_conditioned")
        self.assertIn("Only the first ordered usable image", strategy["honesty_notice"])

    def test_compiler_keeps_reference_honest_when_text_adapter_has_no_reference_declaration(self):
        reference = {"name": "bike.png", "preview_data_url": "data:image/jpeg;base64,abc"}
        state = create_demo_state("A red e-bike unfolds in a rainy alley.", reference)
        PromptCompiler(capability_registry=VideoCapabilityRegistry(text_model="doubao-seedance-1-0-pro")).compile(state)
        generation = state.creative_spec.generation_contract
        self.assertEqual(generation["provider_binding"]["intended_mode"], "text_to_video")
        self.assertEqual(generation["reference_strategy"]["mode"], "reference_retained_not_applied")

    def test_prompt_compiler_creates_traceable_generation_and_evaluation_contracts(self):
        state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley. Keep the logo consistent.")
        spec = state.creative_spec
        generation = spec.generation_contract
        evaluation = spec.evaluation_contract
        self.assertTrue(spec.control_plan_id.startswith("control_"))
        self.assertTrue(spec.compile_run_id.startswith("compile_"))
        self.assertEqual(generation["generation_contract_id"], evaluation["generation_contract_id"])
        self.assertEqual(generation["compile_run_id"], evaluation["compile_run_id"])
        mapped = {item["constraint"] for item in generation["constraint_trace"]}
        self.assertTrue(set(spec.immutable_character_attributes).issubset(mapped))
        self.assertEqual(
            evaluation["evidence_plan"]["sampling_policy"],
            spec.evidence_sampling,
        )

    def test_repair_compiler_scopes_next_generation_to_the_evidence_backed_target(self):
        self.agent.run_once(self.state)
        generation = self.state.creative_spec.generation_contract
        delta = generation["repair_delta"]
        self.assertEqual(generation["target_version_id"], "v2")
        self.assertEqual(delta["mode"], "targeted_repair")
        self.assertEqual(delta["execution_scope"], "whole_clip_regeneration")
        self.assertEqual(delta["target_shot"], self.state.latest_repair_task.target_shot)
        self.assertTrue(delta["preserve_set"])
        self.assertTrue(delta["change_set"])
        self.assertIn("Do not alter passed acceptance criteria outside the targeted repair.", delta["rejected_scope"])
        self.assertIn("complete-clip regeneration", self.state.latest_repair_task.scope_notice)

    def test_generation_adapter_reads_the_compiled_contract(self):
        state = self.state
        expected_objective = state.creative_spec.generation_contract["objective"]
        state.creative_spec.goal = "This uncompiled objective must never reach the provider."
        prompt = ArkVideoGenerator._generation_prompt(state, None)
        self.assertIn(expected_objective, prompt)
        self.assertNotIn("This uncompiled objective", prompt)

    def test_rubric_profile_routes_product_and_action_briefs_to_distinct_observation_focus(self):
        product_state = create_demo_state("A commercial product ad shows a black earbud case opening with its logo readable.")
        action_state = create_demo_state("A woman catches a falling glass and safely places it back on the table.")
        self.assertEqual(product_state.creative_spec.rubric_profile["profile_id"], "product_ad")
        self.assertEqual(action_state.creative_spec.rubric_profile["profile_id"], "action_short")
        mv_state = create_demo_state("An AI music video for a rap artist performing in a neon office.")
        self.assertEqual(mv_state.creative_spec.rubric_profile["profile_id"], "music_video_visual")
        self.assertIn("Audio, lyrics", mv_state.creative_spec.rubric_profile["observation_focus"])
        self.assertIn("camera_control", mv_state.creative_spec.rubric_profile["release_dimensions"])
        product_criteria = product_state.creative_spec.evaluation_contract["acceptance_criteria"]
        self.assertTrue(all(item.get("rubric_dimension") for item in product_criteria))

    def test_rubric_scope_follows_actual_beats_and_criteria(self):
        """A static one-beat plan is not held to an invented continuity gate."""
        static_state = create_demo_state("A commercial close-up of one ceramic perfume bottle with a readable logo.")
        static_profile = static_state.creative_spec.rubric_profile
        self.assertEqual(static_profile["evidence_policy"]["required_beat_ids"], ["S1"])
        self.assertNotIn("narrative_continuity", static_profile["release_dimensions"])
        self.assertNotIn("camera_control", static_profile["release_dimensions"])
        checks = [
            {**criterion, "status": "pass", "evidence": [{"frame_id": "F1", "timestamp": 1.0}]}
            for criterion in static_state.creative_spec.evaluation_contract["acceptance_criteria"]
        ]
        report = EvaluationReport(
            version_id="v1",
            # Motion and continuity are intentionally low diagnostics here;
            # they are not applicable to a single static product detail.
            scores=RubricScores(4.0, 1.0, 1.0, 4.0, 1.0, 0.0),
            bad_cases=[],
            summary="The static product requirement is visibly met.",
            atomic_checks=checks,
        )
        self.assertEqual(apply_release_decision(report, static_profile).release_decision, "pass")

        sequence_profile = self.state.creative_spec.rubric_profile
        self.assertEqual(sequence_profile["evidence_policy"]["required_beat_ids"], ["S1", "S2", "S3"])
        self.assertIn("narrative_continuity", sequence_profile["release_dimensions"])

    def test_action_profile_always_activates_motion_and_physics(self):
        state = create_demo_state("A woman bites an ice cream bar and smiles at the camera.")
        profile = state.creative_spec.rubric_profile
        self.assertEqual(profile["profile_id"], "action_short")
        self.assertIn("camera_control", profile["release_dimensions"])
        criterion = {
            "requirement": "The woman visibly bites the ice cream bar.",
            "expected": "The bite and physical contact are coherent at the key moment.",
        }
        self.assertEqual(dimension_for_criterion(criterion, profile), "camera_control")

    def test_audio_gate_is_conditional_and_compiled_with_an_audio_presence_criterion(self):
        silent_state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley.")
        self.assertEqual(silent_state.creative_spec.evaluation_contract["audio_evaluation"]["mode"], "disabled")
        self.assertFalse(any(item.get("evidence_mode") == "audio_probe" for item in silent_state.creative_spec.evaluation_contract["acceptance_criteria"]))

        audio = {"name": "beat.wav", "media_type": "audio", "data_url": "data:audio/wav;base64,beat"}
        audio_state = create_demo_state("A product reveal to the supplied music.", multimodal_assets=[audio])
        contract = audio_state.creative_spec.evaluation_contract
        self.assertEqual(contract["audio_evaluation"]["mode"], "presence")
        criterion = next(item for item in contract["acceptance_criteria"] if item.get("evidence_mode") == "audio_probe")
        self.assertTrue(criterion["critical"])
        self.assertEqual(criterion["rubric_dimension"], "audio_presence_integrity")

    def test_compiler_adds_non_critical_visual_policy_checks(self):
        action_state = create_demo_state("A woman catches a falling glass and safely places it on a table.")
        criteria = action_state.creative_spec.evaluation_contract["acceptance_criteria"]
        visual = [item for item in criteria if item.get("requirement_id", "").startswith("quality_visual_integrity_")]
        camera = [item for item in criteria if item.get("requirement_id", "").startswith("quality_camera_readability_")]
        self.assertEqual(len(visual), len(action_state.creative_spec.shots))
        self.assertEqual(len(camera), len(action_state.creative_spec.shots))
        self.assertTrue(all(item["critical"] is False for item in visual + camera))

    def test_audio_presence_probe_reads_mp4_audio_track_metadata(self):
        def atom(kind, payload):
            return (len(payload) + 8).to_bytes(4, "big") + kind + payload

        def mdhd(timescale, duration):
            return b"\x00\x00\x00\x00" + b"\x00" * 8 + timescale.to_bytes(4, "big") + duration.to_bytes(4, "big")

        def track(kind, seconds):
            handler = b"\x00" * 8 + kind + b"\x00" * 4
            media = atom(b"hdlr", handler) + atom(b"mdhd", mdhd(1000, int(seconds * 1000)))
            return atom(b"trak", atom(b"mdia", media))

        blob = atom(b"ftyp", b"isom") + atom(b"moov", track(b"vide", 5) + track(b"soun", 5))
        result = AudioPresenceProbe._mp4_audio_metadata(blob)
        self.assertEqual(result["audio_track_count"], 1)
        self.assertEqual(result["video_duration_seconds"], 5.0)
        self.assertEqual(result["audio_duration_seconds"], 5.0)

    def test_rubric_release_requires_evidence_overall_and_dimension_gates(self):
        state = create_demo_state("A commercial product ad shows a black earbud case with a readable logo.")
        checks = [
            {**criterion, "status": "pass", "evidence": [{"frame_id": "F1"}]}
            for criterion in state.creative_spec.evaluation_contract["acceptance_criteria"]
        ]
        report = EvaluationReport(
            version_id="v1",
            scores=RubricScores(4.0, 4.0, 4.0, 4.0, 4.0, 0.0),
            bad_cases=[],
            summary="All critical product requirements are visibly supported.",
            atomic_checks=checks,
        )
        applied = apply_release_decision(report, state.creative_spec.rubric_profile)
        self.assertEqual(applied.release_decision, "pass")
        self.assertGreater(applied.diagnostic_overall, 0)
        at_release_floor = EvaluationReport(
            version_id="v1",
            scores=RubricScores(3.0, 3.0, 3.0, 3.0, 3.0, 0.0),
            bad_cases=[],
            summary="All critical requirements are evidenced at the usable release floor.",
            atomic_checks=[{**item} for item in checks],
        )
        self.assertEqual(apply_release_decision(at_release_floor, state.creative_spec.rubric_profile).release_decision, "pass")
        below_target = EvaluationReport(
            version_id="v1",
            scores=RubricScores(2.9, 2.9, 2.9, 2.9, 2.9, 0.0),
            bad_cases=[],
            summary="Evidence is complete but diagnostic quality is below the release target.",
            atomic_checks=[{**item} for item in checks],
        )
        self.assertEqual(apply_release_decision(below_target, state.creative_spec.rubric_profile).release_decision, "fail")
        checks[0]["status"] = "fail"
        blocked = apply_release_decision(report, state.creative_spec.rubric_profile)
        self.assertEqual(blocked.release_decision, "fail")

    def test_vision_evaluation_json_maps_to_rubric_and_bad_cases(self):
        evaluator = ArkVisionEvaluator(api_key="test-key", model="test-model")
        # This test isolates evaluation JSON mapping against the explicit
        # three-beat sample fixture. Other tests prove live plans can have a
        # different number of beats.
        state = self.state
        report = evaluator._to_report(state, {
            "scores": {
                "instruction_following": 2.2,
                "character_consistency": 4.1,
                "camera_control": 3.4,
                "visual_quality": 3.7,
                "narrative_continuity": 2.6,
            },
            "atomic_checks": [
                {"requirement_id": "s1_intent_visible", "status": "pass", "observed": "The setting is visible.", "evidence": [{"frame_id": "F1", "timestamp": 1.0, "description": "Opening scene."}]},
                {"requirement_id": "s2_intent_visible", "status": "fail", "observed": "The e-bike is still folded.", "evidence": [{"frame_id": "F2", "timestamp": 2.0, "description": "Action not complete."}], "repair_hint": "Show the completed unfolding."},
                {"requirement_id": "s3_intent_visible", "status": "pass", "observed": "Final result visible.", "evidence": [{"frame_id": "F3", "timestamp": 3.0, "description": "Final shot."}]},
                {"requirement_id": "cross_shot_constraints", "status": "pass", "observed": "Constraints maintained.", "evidence": [{"frame_id": "F3", "timestamp": 3.0, "description": "Final continuity."}]},
            ],
            "summary": "The core action is not visibly completed, so repair is required.",
        }, {"F1": "S1", "F2": "S2", "F3": "S3"})
        self.assertEqual(report.observation_source, "doubao_seed_2_lite")
        self.assertEqual(report.bad_cases[0].severity.value, "blocking")
        self.assertEqual(report.scores.instruction_following, 2.2)

    def test_vision_bad_case_shot_labels_are_normalized(self):
        evaluator = ArkVisionEvaluator(api_key="test-key", model="test-model")
        frame_map = {"F3": "S2", "F6": "S3"}
        self.assertEqual(evaluator._normalise_shot_id("Shot 2 (action)", "F3 at 2.1s", frame_map), "S2")
        self.assertEqual(evaluator._normalise_shot_id("", "F6 at 5.4s shows a continuity break.", frame_map), "S3")
        self.assertIsNone(evaluator._normalise_shot_id("final scene", "No frame cited."))

    def test_missing_critical_frame_evidence_cannot_be_published(self):
        state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley.")
        report = ArkVisionEvaluator(api_key="test-key", model="test-model")._to_report(state, {
            "scores": {dimension: 5.0 for dimension in ["instruction_following", "character_consistency", "camera_control", "visual_quality", "narrative_continuity"]},
            "atomic_checks": [
                {"requirement_id": "s1_intent_visible", "status": "pass", "observed": "Opening visible.", "evidence": [{"frame_id": "F1", "timestamp": 1.0, "description": "Opening."}]},
                {"requirement_id": "s2_intent_visible", "status": "pass", "observed": "Action completed.", "evidence": []},
                {"requirement_id": "s3_intent_visible", "status": "pass", "observed": "Final visible.", "evidence": [{"frame_id": "F3", "timestamp": 3.0, "description": "Final."}]},
                {"requirement_id": "cross_shot_constraints", "status": "pass", "observed": "Constraints visible.", "evidence": [{"frame_id": "F3", "timestamp": 3.0, "description": "Continuity."}]},
            ],
            "summary": "All dimensions are high, but one cited acceptance result is unsupported.",
        }, {"F1": "S1", "F2": "S2", "F3": "S3"})
        self.assertEqual(report.release_decision, "insufficient_evidence")
        self.assertEqual(report.atomic_checks[1]["status"], "insufficient_evidence")

    def test_repair_planner_normalizes_single_text_list_fields(self):
        from backend.app.agent.planner import DeepSeekPlanner

        self.assertEqual(DeepSeekPlanner._string_list("camera_control", "expected_improvement"), ["camera_control"])
        self.assertEqual(DeepSeekPlanner._string_list(["instruction_following"], "expected_improvement"), ["instruction_following"])

    def test_seed_responses_output_is_extracted(self):
        self.assertEqual(
            ArkVisionEvaluator._response_text({"output": [{"content": [{"type": "output_text", "text": "{\"summary\": \"ok\"}"}]}]}),
            '{"summary": "ok"}',
        )

    def test_user_video_never_uses_fixture_when_vision_is_unavailable(self):
        class UnavailableVision:
            def available(self):
                return False

        state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley.")
        state.current_video_asset = {"source": "user_upload", "name": "ebike.mp4"}
        state.evaluation_frame_records = [{"frame_id": "F1", "timestamp": 0.0, "path": "/tmp/unused.jpg"}]
        evaluator = ResilientVisionEvaluator(primary=UnavailableVision())
        with self.assertRaises(VisionObservationUnavailable):
            evaluator.analyze(state)

    def test_autopilot_pauses_after_visual_observation_failure(self):
        class FailingObservation:
            def analyze(self, _state):
                raise VisionObservationUnavailable("Vision output is unavailable")

        state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley.")
        state.current_version_id = "v1"
        state.current_video_asset = {"source": "agent_generated", "name": "v1.mp4"}
        state.version_reviews = [VersionReview(version_id="v1", parent_version_id=None)]
        state.execution_mode = "autopilot"
        state.autopilot_paused = False
        self.agent.evaluator = FailingObservation()

        self.agent.run_once(state)

        self.assertEqual(state.status, "ready_for_evaluation")
        self.assertTrue(state.autopilot_paused)
        self.assertTrue(any(item["event"] == "autopilot_paused_for_observation_failure" for item in state.history))

    def test_repair_task_uses_current_control_plan_not_fixture_story(self):
        state = create_demo_state("A red e-bike unfolds in a rainy Tokyo alley.")
        report = ArkVisionEvaluator(api_key="test-key", model="test-model")._to_report(state, {
            "scores": {dimension: 3.0 for dimension in ["instruction_following", "character_consistency", "camera_control", "visual_quality", "narrative_continuity"]},
            "atomic_checks": [
                {"requirement_id": "s1_intent_visible", "status": "pass", "observed": "Opening visible.", "evidence": [{"frame_id": "F1", "timestamp": 1.0, "description": "Opening."}]},
                {"requirement_id": "s2_intent_visible", "status": "fail", "observed": "The e-bike is still folded.", "evidence": [{"frame_id": "F2", "timestamp": 2.0, "description": "Folded."}]},
                {"requirement_id": "s3_intent_visible", "status": "pass", "observed": "Final visible.", "evidence": [{"frame_id": "F3", "timestamp": 3.0, "description": "Final."}]},
                {"requirement_id": "cross_shot_constraints", "status": "pass", "observed": "Constraints visible.", "evidence": [{"frame_id": "F3", "timestamp": 3.0, "description": "Continuity."}]},
            ],
            "summary": "The e-bike unfolding action is incomplete.",
        }, {"F1": "S1", "F2": "S2", "F3": "S3"})
        state.latest_report = report
        from backend.app.domain.models import Action, Decision
        task = rewrite_generation_task(state, Decision(Action.SPLIT_SHOT, "S2", "high", "Repair action", ["instruction_following"], [], "rewrite_generation_task"))
        self.assertIn("e-bike", task.prompt)
        self.assertNotIn("detective", task.prompt)


if __name__ == "__main__":
    unittest.main()
