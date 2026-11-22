import http.client
import hashlib
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app import server
from backend.app.demo import create_demo_state
from backend.app.domain.models import GenerationJob
from backend.app.sessions import SessionStore, encode


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name))
        self.patcher = patch.object(server, "SESSIONS", self.store)
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

    def session(self):
        self.request("/api/state")
        return self.cookie.split("=", 1)[1].split(".")[0]

    def plan(self, visitor):
        state = create_demo_state("A cat resting on a sofa for 15 seconds.")
        state.status, state.current_version_id = "awaiting_first_video", "none"
        # Match request serialization: an earlier response may still be finishing.
        lock = server.VISITOR_LOCKS[int(hashlib.sha256(visitor.encode()).hexdigest(), 16) % len(server.VISITOR_LOCKS)]
        with lock:
            self.store.save(visitor, state)

    def test_planning_has_no_trial_gate(self):
        state = create_demo_state()
        with patch.object(server, "create_enriched_project_state", return_value=state) as planner:
            status, data = self.request("/api/create-project", {"prompt": "a cat"})
        self.assertEqual(status, 200)
        planner.assert_called_once()
        self.assertNotIn("trial", data)
        self.assertNotIn("demo_mode", data)

    def test_submitted_conversation_survives_replan_refresh_and_reset(self):
        def plan(prompt, *args, **kwargs):
            state = create_demo_state(prompt)
            state.status, state.current_version_id = "planning_ready", "none"
            return state

        with patch.object(server, "create_enriched_project_state", side_effect=plan):
            _, first = self.request("/api/create-project", {"prompt": "A tea advertisement."})
            status, revised = self.request("/api/revise-brief", {"instruction": "Use warm light."})
        self.assertEqual(status, 200)
        self.assertEqual(revised["conversation"][:2], first["conversation"])
        self.assertEqual([m["text"] for m in revised["conversation"] if m["role"] == "user"], ["A tea advertisement.", "Use warm light."])
        self.assertEqual(self.request("/api/state")[1]["conversation"], revised["conversation"])
        self.assertEqual(self.request("/api/reset", {})[1]["conversation"], [])

    def test_context_confirmation_retains_earlier_question_and_brief(self):
        initial = create_demo_state("A seasonal tea advertisement.")
        initial.status = "awaiting_context_confirmation"
        initial.creative_spec.enriched_context["open_questions"] = [{"question": "Which region?", "blocking": True}]
        ready = create_demo_state(initial.creative_spec.source_prompt)
        ready.status = "planning_ready"
        with patch.object(server, "create_enriched_project_state", side_effect=[initial, ready]):
            _, first = self.request("/api/create-project", {"prompt": initial.creative_spec.source_prompt})
            status, confirmed = self.request("/api/confirm-context", {"target_market": "Singapore"})
        self.assertEqual(status, 200)
        self.assertEqual(first["conversation"][-1]["kind"], "context_question")
        self.assertEqual(confirmed["conversation"][:2], first["conversation"])
        self.assertEqual(confirmed["conversation"][2]["text"], "Singapore")

    def test_legacy_pending_direction_does_not_duplicate_the_new_message(self):
        visitor = self.session()
        state = create_demo_state("A tea advertisement.")
        state.status = "ready_for_evaluation"
        lock = server.VISITOR_LOCKS[int(hashlib.sha256(visitor.encode()).hexdigest(), 16) % len(server.VISITOR_LOCKS)]
        with lock:
            self.store.save(visitor, state)
        status, updated = self.request("/api/revise-brief", {"instruction": "Keep the label clear."})
        self.assertEqual(status, 200)
        self.assertEqual([m["text"] for m in updated["conversation"] if m["role"] == "user"], ["A tea advertisement.", "Keep the label clear."])

    def test_interface_language_is_forwarded_for_creation_and_revision(self):
        def plan(prompt, *args, **kwargs):
            state = create_demo_state(prompt)
            state.status = "planning_ready"
            return state
        with patch.object(server, "create_enriched_project_state", side_effect=plan) as planner:
            self.request("/api/create-project", {"prompt":"mean girl scene", "language":"en"})
            self.assertEqual(planner.call_args.kwargs["ui_language"], "en")
            self.request("/api/revise-brief", {"instruction":"保留 gap", "language":"zh"})
            self.assertEqual(planner.call_args.kwargs["ui_language"], "zh")

    def test_more_than_thirty_seconds_can_generate_without_codes(self):
        visitor = self.session()
        video = Path(self.tmp.name) / "test.mp4"
        video.write_bytes(b"fake test video")
        for index in range(3):
            self.plan(visitor)
            job = GenerationJob(f"success-{index}", "v1", "fake", "fake", "prompt")
            with patch.object(server.VIDEO_GENERATOR, "available", return_value=True), patch.object(server.VIDEO_GENERATOR, "select_tool", return_value={}), patch.object(server.VIDEO_GENERATOR, "submit", return_value=job):
                status, data = self.request("/api/generate-video", {})
            self.assertEqual(status, 202, data)
            self.assertNotIn("trial", data)
            job.status, job.output_video_url = "succeeded", "https://example.invalid/video.mp4"
            with patch.object(server.VIDEO_GENERATOR, "refresh", return_value=job), patch.object(server.DemoHandler, "_download_generated_video", return_value=(video, video.stat().st_size)):
                _, data = self.request("/api/refresh-generation", {})
            self.assertEqual(data["status"], "generated_video_ready")
            self.assertNotIn("local_video_path", data["current_video_asset"])
            self.request("/api/reset", {})

    def test_duplicate_generation_and_reset_are_blocked_while_pending(self):
        visitor = self.session()
        self.plan(visitor)
        job = GenerationJob("pending", "v1", "fake", "fake", "prompt")
        with patch.object(server.VIDEO_GENERATOR, "available", return_value=True), patch.object(server.VIDEO_GENERATOR, "select_tool", return_value={}), patch.object(server.VIDEO_GENERATOR, "submit", return_value=job) as submit:
            self.assertEqual(self.request("/api/generate-video", {})[0], 202)
            self.assertEqual(self.request("/api/generate-video", {})[0], 400)
            self.assertEqual(self.request("/api/reset", {})[0], 409)
            submit.assert_called_once()

    def test_sessions_are_isolated_and_persist_after_restart(self):
        visitor = self.session()
        self.plan(visitor)
        original_cookie = self.cookie
        self.cookie = ""
        _, other = self.request("/api/state")
        self.assertEqual(other["status"], "ready_for_brief")
        restored = SessionStore(Path(self.tmp.name))
        self.assertEqual(restored.load(visitor).status, "awaiting_first_video")
        self.cookie = original_cookie
        _, original = self.request("/api/state")
        self.assertEqual(original["status"], "awaiting_first_video")

    def test_old_cookie_and_removed_field_preserve_real_project(self):
        visitor = self.session()
        state = create_demo_state()
        saved = encode(state)
        saved["fields"]["demo_mode"] = False
        with self.store.connect() as db:
            db.execute("UPDATE visitors SET state=? WHERE id=?", (json.dumps(saved), visitor))
        self.cookie = self.cookie.replace("agent_session=", "trial_visitor=")
        status, data = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(data["creative_spec"]["source_prompt"], state.creative_spec.source_prompt)
        self.assertTrue(self.cookie.startswith("agent_session="))

    def test_synthetic_public_demo_is_not_restored(self):
        visitor = self.session()
        saved = encode(create_demo_state())
        saved["fields"]["demo_mode"] = True
        with self.store.connect() as db:
            db.execute("UPDATE visitors SET state=? WHERE id=?", (json.dumps(saved), visitor))
        _, data = self.request("/api/state")
        self.assertEqual(data["status"], "ready_for_brief")

    def test_retired_public_endpoints_are_absent(self):
        for path in ("/api/trial/verify", "/api/demo", "/api/simulate-repair"):
            self.assertEqual(self.request(path, {})[0], 404)

    def test_cross_site_and_nonlocal_hosts_are_rejected(self):
        self.assertEqual(self.request("/api/reset", {}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/reset", {}, {"Content-Type": "text/plain"})[0], 415)


class LocalSessionMigrationTests(unittest.TestCase):
    def test_old_lock_and_exhausted_allowance_do_not_limit_local_use(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "trial.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE visitors(id TEXT PRIMARY KEY, failures INTEGER DEFAULT 0, code TEXT, state TEXT);")
            store = SessionStore(Path(temp))
            visitor, cookie = store.visitor("")
            state = create_demo_state()
            store.save(visitor, state)
            with store.connect() as db:
                db.execute("UPDATE visitors SET failures=5,code='retired' WHERE id=?", (visitor,))
            restored, _ = store.visitor(cookie)
            self.assertEqual(restored, visitor)
            self.assertEqual(store.load(visitor).creative_spec.source_prompt, state.creative_spec.source_prompt)
