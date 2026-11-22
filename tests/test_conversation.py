import unittest
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
        self.assertIn(revised.creative_spec.shots[0]["intent"], revised.conversation[-1]["text"])
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
