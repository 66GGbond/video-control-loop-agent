import base64
import unittest
from unittest.mock import patch
from backend.app.reference_imports import skill_files
from backend.app.demo import create_demo_state,create_enriched_project_state
from backend.app.agent.asset_manifest import build_asset_manifest
from backend.app import server
from tests import test_local_server as local

class ImportTests(unittest.TestCase):
    def test_skill_documents_are_bounded_and_keep_relative_paths(self):
        docs=[{'path':'western/SKILL.md','text':'Use warm colors.'},{'path':'western/palette.txt','text':'Earth brown'}]
        self.assertEqual(skill_files(docs),docs)
        for value in [[{'path':'../secret','text':'x'}],[{'path':'code.py','text':'print(1)'}],[{'path':'SKILL.md','text':'x'*96001}]]:
            with self.assertRaises(ValueError):skill_files(value)

    def test_uploaded_skill_text_is_passed_to_the_decision_planner(self):
        from backend.app.agent.planner import ArkControlPlanner
        planner=ArkControlPlanner(api_key='offline')
        docs=[{'path':'western/SKILL.md','text':'Use warm earth colors.'}]
        with patch('backend.app.agent.planner.request_json',return_value={}) as model,patch.object(planner,'_to_spec',return_value=create_demo_state('film').creative_spec):
            planner.plan('film',enriched_context={'uploaded_skills':docs})
        self.assertEqual(model.call_args.args[3]['user_imported_skills'],docs)

class ImportHTTPTests(unittest.TestCase):
    setUp=local.LocalServerTests.setUp
    cleanup=local.LocalServerTests.cleanup
    request=local.LocalServerTests.request
    session=local.LocalServerTests.session

    def test_legacy_story_region_gate_resumes_same_project_without_generation(self):
        visitor=self.session();state=create_demo_state('1784年英国春季，中文对白，画眉田庄卧室。');state.status='awaiting_context_confirmation'
        state.conversation=[{'role':'user','text':state.creative_spec.source_prompt},{'role':'assistant','kind':'context_question','text':'Which region?'}]
        self.store.save(visitor,state)
        replanned=create_demo_state(state.creative_spec.source_prompt)
        with patch.object(server,'create_enriched_project_state',return_value=replanned),patch.object(server.VIDEO_GENERATOR,'submit') as paid:
            status,data=self.request('/api/state')
        self.assertEqual(status,200,data);paid.assert_not_called()
        self.assertEqual(data['project_id'],state.project_id)
        self.assertEqual(data['status'],'planning_ready')
        self.assertFalse(any(m.get('kind')=='context_question' for m in data['conversation']))

    def test_append_images_skills_and_local_preview_preserves_existing_video(self):
        visitor=self.session();state=create_demo_state('Cowboy film');state.status='published'
        state.current_video_asset={'name':'original.mp4'};self.store.save(visitor,state)
        video={'name':'reference.mp4','mime_type':'video/mp4','local_only':True,'data_url':'data:video/mp4;base64,'+base64.b64encode(b'local-video-fixture').decode(),'duration_seconds':8}
        image={'name':'look.png','mime_type':'image/png','data_url':'data:image/png;base64,aW1hZ2U='}
        docs=[{'path':'western/SKILL.md','text':'Use warm earth colors.'}]
        with patch.object(server.VIDEO_GENERATOR,'submit') as paid:
            status,data=self.request('/api/import-references',{'multimodal_assets':[image,video],'skill_files':docs})
            self.assertEqual(status,200,data);paid.assert_not_called()
        self.assertEqual(data['project_id'],state.project_id)
        self.assertEqual(data['current_video_asset']['name'],'original.mp4')
        self.assertEqual(data['creative_spec']['enriched_context']['uploaded_skills'],docs)
        self.assertNotIn('data_url',data['creative_spec']['multimodal_assets'][1])
        saved=self.store.load(visitor)
        self.assertIn('data_url',saved.creative_spec.multimodal_assets[1])
        self.assertEqual(len(build_asset_manifest(saved.creative_spec.multimodal_assets)),1)
        # Duplicate bindings cannot silently replace the earlier upload.
        self.assertEqual(self.request('/api/import-references',{'multimodal_assets':[image]})[0],400)
        self.assertEqual(len(self.store.load(visitor).creative_spec.multimodal_assets),2)
