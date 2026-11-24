import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.demo import create_empty_state
from backend.app.domain.models import EvaluationReport, RubricScores
from backend.app.review_translation import translate_review
from backend.app.sessions import SessionStore


class ReviewTranslationTests(unittest.TestCase):
    def test_archived_review_translation_is_cached_per_language_and_content(self):
        report = EvaluationReport(
            "v1", RubricScores(3, 3, 3, 3, 3, 0), [], "A stable shot.",
            atomic_checks=[{
                "requirement": "主体不动",
                "expected": "主体不动",
                "status": "pass",
                "evidence": [{"frame_id": "F1", "timestamp": 1.0, "description": "A stable shot."}],
            }],
        )
        zh = {"0": "镜头稳定。", "1": "主体不动"}
        en = {"0": "A stable shot.", "1": "The subject stays still."}
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            owner, _ = store.visitor("")
            state = create_empty_state()
            state.project_name = "Test project"
            store.save(owner, state)
            project_id = state.project_id
            with patch.dict("os.environ", {"ARK_API_KEY": "test-key"}), patch(
                "backend.app.review_translation.request_json", side_effect=[zh, en, {"0": "partial"}],
            ) as model:
                self.assertEqual(translate_review(store, owner, project_id, report, "zh")["A stable shot."], zh["0"])
                self.assertEqual(translate_review(store, owner, project_id, report, "en")["主体不动"], en["1"])
                self.assertEqual(translate_review(store, owner, project_id, report, "zh")["A stable shot."], zh["0"])
                self.assertEqual(model.call_count, 2)
                self.assertEqual(report.atomic_checks[0]["evidence"][0]["description"], "A stable shot.")
                report.summary = "Re-evaluated shot."
                with self.assertRaisesRegex(ValueError, "Incomplete review translation"):
                    translate_review(store, owner, project_id, report, "zh")
