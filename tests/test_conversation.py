import unittest
from unittest.mock import patch
from backend.app.agent.planner import ArkControlPlanner
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.conversation import messages, record_exchange
from backend.app.demo import create_demo_state, present
from backend.app.sessions import SessionStore, decode, encode


class ConversationTests(unittest.TestCase):
    def test_legacy_project_recovers_retained_directions_without_inventing_replies(self):
        state = create_demo_state("An orchard.\n\nAdditional user direction: Use warm light.")
        state.pending_user_direction = "Make the label clearer."
        saved = encode(state)
        saved["fields"].pop("conversation")
        restored = decode(saved)
        turns = present(restored)["conversation"]
        self.assertEqual([m["text"] for m in turns], ["An orchard.", "Use warm light.", "Make the label clearer."])
        self.assertTrue(all(m["role"] == "user" for m in turns))

    def test_actual_reply_and_submitted_text_survive_store_restart(self):
        initial = create_demo_state("A tea advertisement.")
        record_exchange(initial, "A tea advertisement.")
        first = messages(initial)
        revised = create_demo_state("A tea advertisement. Use warm light.")
        record_exchange(revised, "Use warm light.", initial)
        self.assertEqual(revised.conversation[:2], first)
        self.assertNotIn(revised.creative_spec.shots[0]["intent"], revised.conversation[-1]["text"])
        self.assertIn(revised.conversation[-1]["kind"], {"plan_updated", "plan_fallback"})
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            visitor, _ = store.visitor("")
            store.save(visitor, revised)
            self.assertEqual(SessionStore(Path(directory)).load(visitor).conversation, revised.conversation)

    def test_same_state_update_keeps_prior_turns_once(self):
        state = create_demo_state("A tea advertisement.")
        record_exchange(state, "A tea advertisement.")
        record_exchange(state, "Keep the cup visible.", state, reply="Direction saved for the next eligible version.")
        self.assertEqual([m["text"] for m in state.conversation if m["role"] == "user"], ["A tea advertisement.", "Keep the cup visible."])
        self.assertEqual(len({m["id"] for m in state.conversation}), 4)

    def test_legacy_plan_is_condensed_only_in_projection_without_changing_user_words(self):
        state = create_demo_state("mean girl campus scene")
        state.conversation = [{"id":"u", "role":"user", "text":"mean girl gap"}, {"id":"a", "role":"assistant", "text":"Doubao Seed 2.1 Pro converted this brief into a structured control plan.\nS1: Detailed shot"}]
        projected = messages(state)
        self.assertEqual(projected[0]["text"], "mean girl gap")
        self.assertEqual(projected[1]["kind"], "plan_ready")
        self.assertNotIn("Detailed shot", projected[1]["text"])
        self.assertIn("Detailed shot", state.conversation[1]["text"])
        record_exchange(state,"Change the lighting",state)
        self.assertIn("Detailed shot",state.conversation[1]["text"])

    def test_planning_uses_interface_language_and_preserves_quoted_user_words(self):
        for locale, name in (("zh", "Simplified Chinese"), ("en", "English")):
            with self.subTest(locale=locale):
                planner = ArkControlPlanner(api_key="test-only")
                with patch.object(planner, "_request_json", side_effect=RuntimeError("stop before provider")) as request:
                    with self.assertRaises(RuntimeError):
                        planner.plan("mean girl gap 校园反转", enriched_context={"ui_language":locale})
                system, payload, _ = request.call_args.args
                self.assertIn("Interface language: " + name, system)
                self.assertIn("Keep schema keys, IDs and enum values unchanged", system)
                self.assertEqual(payload["creative_brief"], "mean girl gap 校园反转")

    def test_unknown_historic_reply_is_not_replaced_by_a_fabricated_question(self):
        state = create_demo_state("campus scene")
        state.conversation = [{"id":"a", "role":"assistant", "text":"Try changing the camera angle."}]
        self.assertEqual(messages(state), state.conversation)
