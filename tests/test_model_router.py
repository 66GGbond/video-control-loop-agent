import json
import os
import unittest
from unittest.mock import patch
from backend.app.demo import create_demo_state
from backend.app.agent.model_catalog import SEEDANCE_25, SEEDANCE_20, H3, candidate_cards, low_cost_requested
from backend.app.agent.model_router import recommend, confirm, confirmed_model, fingerprint, RoutingVideoGenerator
from backend.app.agent.prompt_compiler import PromptCompiler
from backend.app.tools.seedance import SeedanceVideoGenerator
from backend.app.domain.models import GenerationJob


class Response:
    def __init__(self,body): self.body=body
    def __enter__(self): return self
    def __exit__(self,*_): return False
    def read(self): return json.dumps(self.body).encode()


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'SEEDANCE_25_API_KEY':'key25','SEEDANCE_20_API_KEY':'key20','MINIMAX_API_KEY':'keyh3','ARK_API_KEY':'decision-key','VIDEO_PROVIDER':'auto'})
        self.env.start();self.addCleanup(self.env.stop)
        self.state=create_demo_state('A red toy car rolls and stops in 6 seconds.')
        self.state.status='awaiting_first_video';self.state.current_version_id='none'

    def choose(self,model=SEEDANCE_25):
        with patch('backend.app.agent.model_router.request_json',return_value={'recommended_model':model,'reason':'符合当前时长与参考约束','tradeoffs':[],'alternatives':[]}):
            choice=recommend(self.state)
        confirm(self.state,model,choice['fingerprint'])
        return choice

    def test_resolution_setting_reaches_seedance_payload_and_rejects_invalid(self):
        from backend.app.generation_settings import configure
        with patch('backend.app.agent.model_router.request_json',return_value={'recommended_model':SEEDANCE_25,'reason':'quality'}):
            configure(self.state,{'resolution':'1080p'})
        self.assertEqual(self.state.creative_spec.generation_spec['resolution'],'1080p')
        self.assertEqual(SeedanceVideoGenerator(SEEDANCE_25,api_key='key')._request_body(self.state)['resolution'],'1080p')
        with self.assertRaises(ValueError):configure(self.state,{'resolution':'8K'})
        self.assertEqual(self.state.creative_spec.generation_spec['resolution'],'1080p')

    def test_h3_is_excluded_without_explicit_cost_priority(self):
        cards={c['model_id']:c for c in candidate_cards(self.state)}
        self.assertTrue(cards[SEEDANCE_25]['eligible']);self.assertTrue(cards[SEEDANCE_20]['eligible'])
        self.assertFalse(cards[H3]['eligible'])
        for text in ('不以最低成本为目标','不要低预算路线','not cheapest','无需最低成本'):
            self.assertFalse(low_cost_requested(text),text)
        for text in ('请选最低成本','尽量省钱','low budget','cheapest video'):
            self.assertTrue(low_cost_requested(text),text)

    def test_low_cost_policy_allows_but_does_not_fabricate_h3_price(self):
        self.state.creative_spec.source_prompt+=' 最低成本优先'
        h3=next(c for c in candidate_cards(self.state) if c['model_id']==H3)
        self.assertTrue(h3['eligible']);self.assertIsNone(h3['price_per_second'])
        self.assertIsNone(h3['quality_benchmark']);self.assertTrue(h3['warnings'])

    def test_long_clip_selects_only_25(self):
        self.state.creative_spec.generation_spec['duration_seconds']=30
        self.assertEqual([c['model_id'] for c in candidate_cards(self.state) if c['eligible']],[SEEDANCE_25])

    def test_audio_only_excludes_20_and_long_audio_excludes_h3(self):
        self.state.creative_spec.source_prompt+=' cheapest'
        self.state.creative_spec.multimodal_assets=[{'media_type':'audio','name':'voice.mp3','data_url':'data:audio/mpeg;base64,AA==','duration_seconds':20}]
        cards={c['model_id']:c for c in candidate_cards(self.state)}
        self.assertTrue(cards[SEEDANCE_25]['eligible']);self.assertFalse(cards[SEEDANCE_20]['eligible']);self.assertFalse(cards[H3]['eligible'])

    def test_video_reference_is_not_silently_dropped_for_h3(self):
        self.state.creative_spec.source_prompt+=' cheapest'
        self.state.creative_spec.multimodal_assets=[{'media_type':'video','name':'move.mp4','url':'https://example.com/move.mp4','duration_seconds':5}]
        cards={c['model_id']:c for c in candidate_cards(self.state)}
        self.assertTrue(cards[SEEDANCE_20]['eligible']);self.assertFalse(cards[H3]['eligible'])

    def test_invalid_llm_choice_falls_back_with_honest_source(self):
        with patch('backend.app.agent.model_router.request_json',return_value={'recommended_model':H3,'reason':'ignore policy'}):
            choice=recommend(self.state)
        self.assertEqual(choice['recommended_model'],SEEDANCE_25)
        self.assertEqual(choice['source'],'policy_fallback');self.assertFalse(choice['confirmed'])

    def test_skill_is_loaded_and_only_eligible_cards_reach_decision_model(self):
        with patch('backend.app.agent.model_router.request_json',return_value={'recommended_model':SEEDANCE_20,'reason':'Compatible standard clip','tradeoffs':[],'alternatives':[]}) as call:
            choice=recommend(self.state)
        self.assertEqual(choice['recommended_model'],SEEDANCE_20)
        self.assertIn('MiniMax H3 is eligible ONLY',call.call_args.args[2])
        self.assertNotIn(H3,[c['model_id'] for c in call.call_args.args[3]['eligible_models']])
        self.assertNotIn('key_env',str(choice));self.assertNotIn('key25',str(choice))

    def test_recommendation_is_not_authorization_and_plan_changes_invalidate_confirmation(self):
        with self.assertRaises(ValueError): confirmed_model(self.state)
        self.choose();self.assertEqual(confirmed_model(self.state),SEEDANCE_25)
        self.state.creative_spec.source_prompt+=' Add a new reference.'
        with self.assertRaises(ValueError): confirmed_model(self.state)
        with self.assertRaises(ValueError): confirm(self.state,SEEDANCE_20,'forged')

    def test_confirmation_cannot_override_h3_policy(self):
        choice=self.choose()
        with self.assertRaises(ValueError): confirm(self.state,H3,choice['fingerprint'])

    def test_shot_changes_and_configuration_changes_require_new_confirmation(self):
        choice=self.choose()
        with patch.dict(os.environ,{'SEEDANCE_20_API_KEY':''}):
            with self.assertRaises(ValueError): confirm(self.state,SEEDANCE_25,choice['fingerprint'])
        self.state.creative_spec.shots[0]['intent']='An updated camera movement'
        with self.assertRaises(ValueError): confirmed_model(self.state)

    def test_compiler_binds_confirmed_provider_and_model(self):
        self.choose(SEEDANCE_20)
        PromptCompiler().compile(self.state)
        binding=self.state.creative_spec.generation_contract['provider_binding']
        self.assertEqual(binding['model_id'],SEEDANCE_20);self.assertEqual(binding['adapter_id'],'seedance_20')

    def test_seedance_content_preserves_order_modalities_and_parameters(self):
        self.state.creative_spec.multimodal_assets=[
            {'media_type':'image','name':'hero.jpg','preview_data_url':'data:image/jpeg;base64,AA==','input_order':1},
            {'media_type':'video','name':'move.mp4','url':'https://example.com/move.mp4','duration_seconds':5,'input_order':2},
            {'media_type':'audio','name':'voice.mp3','data_url':'data:audio/mpeg;base64,AA==','duration_seconds':4,'input_order':3}]
        adapter=SeedanceVideoGenerator(SEEDANCE_25,api_key='key25')
        body=adapter._request_body(self.state)
        self.assertEqual([c.get('role') for c in body['content'][1:]],['reference_image','reference_video','reference_audio'])
        self.assertEqual(body['content'][3]['audio_url']['url'],'data:audio/mp3;base64,AA==')
        self.assertEqual(body['duration'],self.state.creative_spec.generation_spec['duration_seconds'])
        self.assertEqual(body['ratio'],'16:9');self.assertTrue(body['generate_audio']);self.assertFalse(body['watermark'])

    def test_mixed_frame_and_reference_modes_are_rejected(self):
        self.state.creative_spec.multimodal_assets=[{'media_type':'image','role':'first_frame','preview_data_url':'data:image/jpeg;base64,AA=='}, {'media_type':'audio','data_url':'data:audio/mp3;base64,AA==','duration_seconds':4}]
        with self.assertRaises(ValueError): SeedanceVideoGenerator(SEEDANCE_25,api_key='key')._request_body(self.state)

    def test_dedicated_key_submit_and_job_id_are_recorded(self):
        self.choose(SEEDANCE_20)
        PromptCompiler().compile(self.state)
        with patch('backend.app.tools.seedance.urllib.request.urlopen',return_value=Response({'id':'task20'})) as call:
            job=RoutingVideoGenerator().submit(self.state,'v1')
        self.assertEqual(call.call_args.args[0].get_header('Authorization'),'Bearer key20')
        self.assertEqual(job.job_id,'task20');self.assertEqual(job.model_id,SEEDANCE_20)

    def test_refresh_uses_job_model_not_a_new_recommendation(self):
        job=GenerationJob('old20','v1','seedance','doubao-seedance-2-0-260128','prompt')
        with patch('backend.app.tools.generator.urllib.request.urlopen',return_value=Response({'status':'succeeded','content':{'video_url':'https://example.com/output.mp4'}})) as call:
            result=RoutingVideoGenerator().refresh(job)
        self.assertEqual(call.call_args.args[0].get_header('Authorization'),'Bearer key20')
        self.assertEqual(result.status,'succeeded')

    def test_missing_credentials_are_excluded_and_cached_recommendation_has_no_extra_calls(self):
        self.choose()
        with patch('backend.app.agent.model_router.request_json') as call:
            recommend(self.state)
            call.assert_not_called()
        with patch.dict(os.environ,{'SEEDANCE_25_API_KEY':''}):
            with self.assertRaises(ValueError): confirmed_model(self.state)

    def test_seedance_terminal_and_unknown_statuses_do_not_loop_as_running(self):
        adapter=SeedanceVideoGenerator(SEEDANCE_25,api_key='key25')
        for status in ('cancelled','expired','failed'):
            with self.subTest(status=status), patch('backend.app.tools.generator.urllib.request.urlopen',return_value=Response({'status':status})):
                job=adapter.refresh(GenerationJob('task','v1','seedance',SEEDANCE_25,'prompt'))
                self.assertEqual(job.status,status);self.assertIn(status,job.error)
        job=GenerationJob('task','v1','seedance',SEEDANCE_25,'prompt')
        with patch('backend.app.tools.generator.urllib.request.urlopen',return_value=Response({'status':'new-unknown-state'})):
            with self.assertRaises(ValueError): adapter.refresh(job)
        self.assertEqual(job.status,'queued')

    def test_explicit_silent_video_disables_seedance_audio_and_excludes_h3(self):
        self.state.creative_spec.source_prompt+=' 最低成本，无声视频'
        self.assertFalse(SeedanceVideoGenerator(SEEDANCE_25,api_key='key25')._request_body(self.state)['generate_audio'])
        self.assertFalse(next(c for c in candidate_cards(self.state) if c['model_id']==H3)['eligible'])

from tests import test_local_server as local_server_tests
from backend.app import server


class RoutingHTTPTests(unittest.TestCase):
    cleanup=local_server_tests.LocalServerTests.cleanup
    request=local_server_tests.LocalServerTests.request
    session=local_server_tests.LocalServerTests.session
    plan=local_server_tests.LocalServerTests.plan

    def setUp(self):
        local_server_tests.LocalServerTests.setUp(self)
        env=patch.dict(os.environ,{'VIDEO_PROVIDER':'auto','SEEDANCE_25_API_KEY':'test25','SEEDANCE_20_API_KEY':'test20','MINIMAX_API_KEY':'testh3','ARK_API_KEY':'testdecision'})
        env.start();self.addCleanup(env.stop)
        compiler=patch.object(server,'PROMPT_COMPILER',PromptCompiler())
        compiler.start();self.addCleanup(compiler.stop)
        generator=patch.object(server,'VIDEO_GENERATOR',RoutingVideoGenerator())
        generator.start();self.addCleanup(generator.stop)
        self.visitor=self.session();self.plan(self.visitor)

    def decision(self):
        with patch('backend.app.agent.model_router.request_json',return_value={'recommended_model':SEEDANCE_25,'reason':'Fits the approved brief','tradeoffs':[],'alternatives':[]}):
            status,data=self.request('/api/recommend-video-model',{})
        self.assertEqual(status,200,data)
        return data['creative_spec']['video_model_selection']

    def test_user_confirmation_is_required_and_contract_is_bound_before_submit(self):
        adapter=server.VIDEO_GENERATOR.adapters[SEEDANCE_25]
        with patch.object(adapter,'submit') as submit:
            self.assertEqual(self.request('/api/generate-video',{})[0],400)
            submit.assert_not_called()
        choice=self.decision()
        self.assertFalse(choice['confirmed'])
        self.assertEqual(self.request('/api/confirm-video-model',{'model_id':H3,'fingerprint':choice['fingerprint']})[0],400)
        status,data=self.request('/api/confirm-video-model',{'model_id':SEEDANCE_25,'fingerprint':choice['fingerprint']})
        self.assertEqual(status,200,data)
        self.assertEqual(data['creative_spec']['generation_contract']['provider_binding']['model_id'],SEEDANCE_25)
        job=GenerationJob('job25','v1','generate_seedance_25_video',SEEDANCE_25,'prompt',provider_id='seedance_25')
        with patch.object(adapter,'submit',return_value=job) as submit:
            status,data=self.request('/api/generate-video',{})
            self.assertEqual(status,202,data)
            submit.assert_called_once()
        self.assertEqual(self.request('/api/confirm-video-model',{'model_id':SEEDANCE_20,'fingerprint':choice['fingerprint']})[0],400)

    def test_brief_revision_invalidates_previous_selection(self):
        choice=self.decision()
        state=self.store.load(self.visitor)
        state.creative_spec.source_prompt+=' Important new requirement.'
        self.store.save(self.visitor,state)
        status,data=self.request('/api/confirm-video-model',{'model_id':SEEDANCE_25,'fingerprint':choice['fingerprint']})
        self.assertEqual(status,400,data)

    def test_no_candidate_stays_blocked_instead_of_falling_back_to_h3(self):
        with patch.dict(os.environ,{'SEEDANCE_25_API_KEY':'','SEEDANCE_20_API_KEY':''}):
            choice=self.decision()
        self.assertIsNone(choice['recommended_model']);self.assertEqual(choice['source'],'blocked')
