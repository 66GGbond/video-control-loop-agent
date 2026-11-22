import concurrent.futures
import http.client
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app.access import AccessError, TrialStore, encode, decode
from backend.app.demo import create_demo_state
from backend.app.domain.models import GenerationJob, Severity
from backend.app import server


class TrialStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"TRIAL_CODES": "2468,1357"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = TrialStore(Path(self.tmp.name))
        self.visitor, self.cookie = self.store.visitor("")
        self.store.verify(self.visitor, "2468")

    def test_two_devices_have_independent_quota_and_signed_identity(self):
        other, _ = self.store.visitor("")
        self.store.verify(other, "2468")
        token = self.store.reserve(self.visitor, 15)
        self.store.attach(token, "job")
        self.store.settle(self.visitor, "job", True)
        self.assertEqual(self.store.status(self.visitor)["remaining_seconds"], 15)
        self.assertEqual(self.store.status(other)["remaining_seconds"], 30)
        restored, _ = self.store.visitor(self.cookie)
        self.assertEqual(restored, self.visitor)
        forged, _ = self.store.visitor(self.cookie[:-1] + "x")
        self.assertNotEqual(forged, self.visitor)

    def test_quota_and_code_survive_restart_and_reverification(self):
        token = self.store.reserve(self.visitor, 15)
        self.store.attach(token, "a")
        self.store.settle(self.visitor, "a", True)
        token = self.store.reserve(self.visitor, 15)
        self.store.attach(token, "b")
        self.store.settle(self.visitor, "b", True)
        restarted = TrialStore(Path(self.tmp.name))
        restarted.verify(self.visitor, "2468")
        self.assertEqual(restarted.status(self.visitor)["remaining_seconds"], 0)
        with self.assertRaises(AccessError):
            restarted.reserve(self.visitor, 4)
        restarted.verify(self.visitor, "1357")
        self.assertEqual(restarted.status(self.visitor)["remaining_seconds"], 30)
        restarted.verify(self.visitor, "2468")
        self.assertEqual(restarted.status(self.visitor)["remaining_seconds"], 0)

    def test_atomic_reservation_and_idempotent_settlement(self):
        def reserve(_):
            try:
                return self.store.reserve(self.visitor, 15)
            except AccessError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(bool(r) for r in results), 1)
        self.store.attach(next(r for r in results if r), "job")
        self.store.settle(self.visitor, "job", True)
        self.store.settle(self.visitor, "job", True)
        self.store.settle(self.visitor, "job", False)
        self.assertEqual(self.store.status(self.visitor)["remaining_seconds"], 15)

    def test_failed_generation_refunds_but_unknown_stays_reserved(self):
        token = self.store.reserve(self.visitor, 15)
        self.assertEqual(self.store.status(self.visitor)["reserved_seconds"], 15)
        self.store.attach(token, "failed")
        self.store.settle(self.visitor, "failed", False)
        self.assertEqual(self.store.status(self.visitor)["remaining_seconds"], 30)
        with self.assertRaises(AccessError):
            self.store.reserve(self.visitor, float("nan"))

    def test_lock_after_five_errors_is_persistent_and_cross_code(self):
        for i in range(5):
            with self.assertRaises(AccessError):
                self.store.verify(self.visitor, "0000")
        self.assertTrue(self.store.status(self.visitor)["locked"])
        restarted = TrialStore(Path(self.tmp.name))
        with self.assertRaises(AccessError) as error:
            restarted.verify(self.visitor, "1357")
        self.assertEqual(error.exception.status, 423)
        self.assertEqual(restarted.status(self.visitor)["attempts_remaining"], 0)

    def test_empty_input_does_not_count_and_success_resets_failures(self):
        with self.assertRaises(AccessError):
            self.store.verify(self.visitor, "")
        self.assertEqual(self.store.status(self.visitor)["attempts_remaining"], 5)
        with self.assertRaises(AccessError):
            self.store.verify(self.visitor, "0000")
        self.store.verify(self.visitor, "2468")
        self.assertEqual(self.store.status(self.visitor)["attempts_remaining"], 5)

    def test_paid_calls_have_separate_server_limit(self):
        with patch.dict(os.environ, {"TRIAL_AI_CALL_LIMIT": "2"}):
            self.store.require(self.visitor, paid=True)
            self.store.require(self.visitor, paid=True)
            with self.assertRaises(AccessError):
                self.store.require(self.visitor, paid=True)

    def test_revoked_code_no_longer_authorizes(self):
        with patch.dict(os.environ, {"TRIAL_CODES": "1357"}):
            self.assertFalse(self.store.status(self.visitor)["authorized"])
            with self.assertRaises(AccessError):
                self.store.require(self.visitor)

    def test_rate_limit_and_persistent_typed_state(self):
        self.store.limit_rate("test", 1)
        with self.assertRaises(AccessError):
            self.store.limit_rate("test", 1)
        state = create_demo_state()
        agent = server.create_orchestrator()
        # Explicit fixture path only; never make paid calls in tests.
        from backend.app.tools.evaluator import FixtureVisionEvaluator
        from backend.app.agent.planner import FixturePlanner
        agent.evaluator, agent.planner = FixtureVisionEvaluator(), FixturePlanner()
        agent.run_once(state)
        self.store.save(self.visitor, state)
        loaded = self.store.load(self.visitor)
        self.assertEqual(loaded.latest_report.bad_cases[0].severity, Severity.BLOCKING)
        self.assertEqual(encode(loaded), encode(state))
        self.assertTrue(loaded.latest_report.scores.overall() > 0)


class TrialHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = TrialStore(Path(self.tmp.name))
        self.env = patch.dict(os.environ, {"TRIAL_CODES": "2468"})
        self.env.start()
        self.patcher = patch.object(server, "TRIAL", self.store)
        self.patcher.start()
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DemoHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.patcher.stop()
        self.env.stop()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port)
        conn.request("GET" if body is None else "POST", path,
                     body=None if body is None else json.dumps(body),
                     headers={"Cookie": self.cookie, "Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        self.cookie = (response.getheader("Set-Cookie") or self.cookie).split(";")[0]
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw)

    def authorize(self):
        self.request("/api/state")
        code, _ = self.request("/api/trial/verify", {"code": "2468"})
        self.assertEqual(code, 200)
        return self.cookie.split("=", 1)[1].split(".")[0]

    def plan(self, visitor):
        state = create_demo_state("A cat resting on a sofa for 15 seconds.")
        state.status = "awaiting_first_video"
        state.current_version_id = "none"
        state.creative_spec.generation_spec["duration_seconds"] = 15
        self.store.save(visitor, state)

    def test_anonymous_paid_routes_blocked_and_demo_free(self):
        with patch.object(server.CONTROL_PLANNER, "plan") as planner:
            status, data = self.request("/api/create-project", {"prompt": "a cat"})
            self.assertEqual(status, 401)
            planner.assert_not_called()
        status, data = self.request("/api/demo", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["evaluation_history"]), 2)
        self.assertFalse(data["trial"]["authorized"])

    def test_visitors_cannot_read_each_others_projects_or_videos(self):
        self.authorize()
        self.request("/api/demo", {})
        first = self.cookie
        self.cookie = ""
        _, data = self.request("/api/state")
        self.assertEqual(data["status"], "ready_for_brief")
        status, _ = self.request("/api/generated-video/v1")
        self.assertEqual(status, 404)
        self.cookie = first
        _, data = self.request("/api/state")
        self.assertEqual(len(data["evaluation_history"]), 2)

    def test_csrf_and_invalid_json_body_rejected(self):
        self.authorize()
        status, _ = self.request("/api/reset", {}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/reset", {}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.request("/api/trial/verify", [])
        self.assertEqual(status, 400)

    def test_generation_reservation_pending_failure_and_reset(self):
        visitor = self.authorize()
        self.plan(visitor)
        generator = server.VIDEO_GENERATOR
        job = GenerationJob("job-1", "v1", "fake", "fake", "prompt")
        with patch.object(generator, "available", return_value=True), patch.object(generator, "select_tool", return_value={}), patch.object(generator, "submit", return_value=job) as submit:
            status, data = self.request("/api/generate-video", {})
            self.assertEqual(status, 202, data)
            self.assertEqual(data["trial"]["reserved_seconds"], 15)
            status, _ = self.request("/api/generate-video", {})
            self.assertEqual(status, 400)
            submit.assert_called_once()
            status, _ = self.request("/api/reset", {})
            self.assertEqual(status, 409)
        with patch.object(generator, "refresh", side_effect=ValueError("polling unavailable")):
            _, data = self.request("/api/refresh-generation", {})
            self.assertEqual(data["trial"]["reserved_seconds"], 15)
        job.status = "failed"
        with patch.object(generator, "refresh", return_value=job):
            _, data = self.request("/api/refresh-generation", {})
            self.assertEqual(data["trial"]["remaining_seconds"], 30)
        token = self.store.reserve(visitor, 15)
        self.store.attach(token, "used")
        self.store.settle(visitor, "used", True)
        _, data = self.request("/api/reset", {})
        self.assertEqual(data["trial"]["remaining_seconds"], 15)

    def test_provider_success_charged_even_if_local_download_fails(self):
        visitor = self.authorize()
        self.plan(visitor)
        state = self.store.load(visitor)
        state.status = "generating_video"
        job = GenerationJob("job", "v1", "fake", "fake", "prompt", status="succeeded", output_video_url="https://example.invalid/video.mp4")
        state.generation_jobs.append(job)
        self.store.save(visitor, state)
        token = self.store.reserve(visitor, 15)
        self.store.attach(token, "job")
        with patch.object(server.VIDEO_GENERATOR, "refresh", return_value=job), patch.object(server.DemoHandler, "_download_generated_video", side_effect=ValueError("download failed")):
            _, data = self.request("/api/refresh-generation", {})
        self.assertEqual(data["trial"]["remaining_seconds"], 15)
        self.assertEqual(data["trial"]["used_seconds"], 15)

    def test_two_successful_videos_exhaust_quota_and_remain_private(self):
        visitor = self.authorize()
        video_dir = Path(self.tmp.name) / "generated"
        video_dir.mkdir()
        video = video_dir / "test.mp4"
        video.write_bytes(b"fake-test-video")
        for index in range(2):
            self.plan(visitor)
            job = GenerationJob(f"success-{index}", "v1", "fake", "fake", "prompt")
            with patch.object(server.VIDEO_GENERATOR, "available", return_value=True), patch.object(server.VIDEO_GENERATOR, "select_tool", return_value={}), patch.object(server.VIDEO_GENERATOR, "submit", return_value=job):
                status, data = self.request("/api/generate-video", {})
                self.assertEqual(status, 202, data)
            job.status, job.output_video_url = "succeeded", "https://example.invalid/video.mp4"
            with patch.object(server.VIDEO_GENERATOR, "refresh", return_value=job), patch.object(server.DemoHandler, "_download_generated_video", return_value=(video, video.stat().st_size)):
                status, data = self.request("/api/refresh-generation", {})
            self.assertEqual(data["status"], "generated_video_ready")
            self.assertEqual(data["trial"]["remaining_seconds"], 15 * (1-index))
            self.assertIn("本次使用 15 秒", data["trial_notice"])
            self.assertNotIn("local_video_path", data["current_video_asset"])
            self.request("/api/reset", {})
        self.plan(visitor)
        with patch.object(server.VIDEO_GENERATOR, "available", return_value=True), patch.object(server.VIDEO_GENERATOR, "select_tool", return_value={}), patch.object(server.VIDEO_GENERATOR, "submit") as submit:
            status, data = self.request("/api/generate-video", {})
            self.assertEqual(status, 403)
            submit.assert_not_called()
        self.assertEqual(data["code"], "quota_exhausted")

    def test_explicit_provider_rejection_releases_quota(self):
        from urllib.error import HTTPError
        visitor = self.authorize()
        self.plan(visitor)
        rejected = ValueError("request rejected")
        rejected.__cause__ = HTTPError("https://example.invalid", 402, "balance", {}, None)
        with patch.object(server.VIDEO_GENERATOR, "available", return_value=True), patch.object(server.VIDEO_GENERATOR, "select_tool", return_value={}), patch.object(server.VIDEO_GENERATOR, "submit", side_effect=rejected):
            status, data = self.request("/api/generate-video", {})
        self.assertEqual(status, 502)
        self.assertEqual(data["code"], "submission_rejected")
        self.assertEqual(data["trial"]["remaining_seconds"], 30)

    def test_uncertain_submission_retains_quota_across_restart(self):
        visitor = self.authorize()
        self.plan(visitor)
        with patch.object(server.VIDEO_GENERATOR, "available", return_value=True), patch.object(server.VIDEO_GENERATOR, "select_tool", return_value={}), patch.object(server.VIDEO_GENERATOR, "submit", side_effect=ValueError("timeout")):
            status, data = self.request("/api/generate-video", {})
        self.assertEqual(status, 503, data)
        self.assertEqual(data["code"], "submission_uncertain")
        self.assertEqual(TrialStore(Path(self.tmp.name)).status(visitor)["reserved_seconds"], 15)
