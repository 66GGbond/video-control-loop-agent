import copy
import os
import unittest
from unittest.mock import patch
from backend.app.demo import create_demo_state
from backend.app.chat_turns import respond,recover_editing_question,decide,pending_question
from tests import test_local_server as local


class ChatTurnTests(unittest.TestCase):
    def test_question_is_answered_without_changing_the_brief_or_pending_choice(self):
        state=create_demo_state('完整剧本，英语对白，30秒。');state.status='awaiting_skill_clarification'
        state.creative_spec.enriched_context['skill_clarification']={'question':'是否允许精简台词？'}
        before=copy.deepcopy(state.creative_spec)
        with patch('backend.app.chat_turns.request_json') as model:
            self.assertTrue(respond(state,'精剪版？你还会剪辑？'))
            model.assert_not_called()
        self.assertEqual(state.creative_spec,before);self.assertEqual(state.status,'awaiting_skill_clarification')
        self.assertIn('还不能',state.conversation[-1]['text']);self.assertEqual(state.conversation[-2]['text'],'精剪版？你还会剪辑？')

    def test_general_question_and_polite_action_request_have_different_routes(self):
        state=create_demo_state('原始需求')
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.chat_turns.request_json',side_effect=[{'intent':'discuss','reply':'是在询问是否精简剧本。'},{'intent':'revise','reply':''}]):
            self.assertTrue(respond(state,'你说的是什么意思？'))
            self.assertFalse(respond(state,'能不能把光线改暖一点？'))
        self.assertEqual(len(state.conversation),2)

    def test_dialogue_model_failure_never_falls_through_to_planning(self):
        state=create_demo_state('原始需求')
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.chat_turns.request_json',side_effect=TimeoutError()):
            self.assertTrue(respond(state,'为什么这样安排？'))
        self.assertEqual(state.creative_spec.source_prompt,'原始需求')

    def test_recover_observed_misroute_once_and_keep_the_old_error_trace(self):
        state=create_demo_state('完整剧本\n\nAdditional user direction: 精剪版？你还会剪辑？');state.status='skill_preparation_failed'
        state.creative_spec.enriched_context['creative_answers']=[{'question':'是否允许精简台词？','answer':'精剪版？你还会剪辑？'}]
        state.conversation=[{'role':'assistant','kind':'planning_incomplete','text':'是否允许精简台词？'},{'role':'user','text':'精剪版？你还会剪辑？'},{'role':'assistant','kind':'planning_incomplete','text':'旧的错误提示'}]
        self.assertTrue(recover_editing_question(state));self.assertFalse(recover_editing_question(state))
        self.assertEqual(state.creative_spec.source_prompt,'完整剧本')
        self.assertEqual(state.creative_spec.enriched_context['creative_answers'],[])
        self.assertEqual(state.creative_spec.enriched_context['recovered_dialogue_turns'][0]['former_failure']['text'],'旧的错误提示')
        self.assertEqual(state.status,'awaiting_skill_clarification')


class ChatTurnHTTPTests(unittest.TestCase):
    setUp=local.LocalServerTests.setUp
    cleanup=local.LocalServerTests.cleanup
    request=local.LocalServerTests.request
    session=local.LocalServerTests.session

    def test_unified_chat_binds_an_answer_without_appending_it_to_the_brief(self):
        visitor=self.session();state=create_demo_state('原始英语剧本，30秒。');state.status='awaiting_skill_clarification'
        state.creative_spec.enriched_context['skill_clarification']={'question':'是否允许保留原意地精简台词？'}
        self.store.save(visitor,state)
        text='可以适当保留原意的基础上缩减对话，让这部分内容在30s的视频里节奏不会过于紧凑'
        decision={'intent':'answer','question_id':pending_question(state)['id'],'summary':'保留原意，精简对白以适配30秒','evidence':text,'dialogue_edit_policy':'condense_preserve_meaning','reply':''}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.chat_turns.request_json',return_value=decision),patch.object(local.server,'create_enriched_project_state',return_value=create_demo_state(state.creative_spec.source_prompt)) as planning:
            status,data=self.request('/api/chat',{'message':text})
        self.assertEqual(status,200,data)
        self.assertEqual(planning.call_args.args[0],state.creative_spec.source_prompt)
        self.assertEqual(planning.call_args.kwargs['creative_answers'][-1]['answer'],text)
        self.assertEqual(planning.call_args.kwargs['resolved_requirements']['dialogue_edit_policy'],'condense_preserve_meaning')

    def test_unified_chat_uses_context_for_plain_text_questions_and_preserves_pending_choice(self):
        visitor=self.session();state=create_demo_state('原始剧本');state.status='awaiting_skill_clarification'
        state.creative_spec.enriched_context['skill_clarification']={'question':'是否精简对白？'};self.store.save(visitor,state)
        decision={'intent':'discuss','question_id':'','summary':'','evidence':'','dialogue_edit_policy':None,'reply':'这里是指调整剧本，没有剪辑现有视频的功能。'}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.chat_turns.request_json',return_value=decision) as model,patch.object(local.server,'create_enriched_project_state') as planning:
            status,data=self.request('/api/chat',{'message':'解释一下刚才的意思'})
        self.assertEqual(status,200);planning.assert_not_called();model.assert_called_once()
        self.assertTrue(data['conversation_only']);self.assertEqual(data['status'],state.status)
        self.assertEqual(data['creative_spec']['source_prompt'],'原始剧本')

    def test_unified_chat_rejects_unbound_answer_without_starting_any_work(self):
        visitor=self.session();state=create_demo_state('原始剧本');self.store.save(visitor,state)
        decision={'intent':'answer','question_id':'invented','summary':'擅自修改','evidence':'好的','dialogue_edit_policy':None,'reply':''}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.chat_turns.request_json',return_value=decision),patch.object(local.server,'create_enriched_project_state') as planning:
            status,data=self.request('/api/chat',{'message':'好的'})
        self.assertTrue(data['conversation_only']);planning.assert_not_called()

    def test_question_does_not_start_skill_planning_images_or_video(self):
        visitor=self.session();state=create_demo_state('英语对白，30秒。');state.status='awaiting_skill_clarification'
        state.creative_spec.enriched_context['skill_clarification']={'question':'是否允许精简台词？'}
        self.store.save(visitor,state)
        with patch.object(local.server,'create_enriched_project_state') as planning,patch.object(local.server.characters,'prepare') as images,patch.object(local.server.VIDEO_GENERATOR,'submit') as video:
            status,data=self.request('/api/revise-brief',{'instruction':'精剪版？你还会剪辑？'})
            planning.assert_not_called();images.assert_not_called();video.assert_not_called()
        self.assertEqual(status,200,data);self.assertTrue(data['conversation_only'])
        self.assertEqual(data['status'],'awaiting_skill_clarification')
        self.assertEqual(data['creative_spec']['source_prompt'],'英语对白，30秒。')
