import base64
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app import character_references as chars
from backend.app.demo import create_demo_state
from backend.app.image_tasks import ImageTasks
from backend.app.sessions import SessionStore
from backend.app.server import DemoHandler
from backend.app.tools.seedream import PRO
from backend.app.tools.seedance import SeedanceVideoGenerator
from backend.app.agent.model_catalog import SEEDANCE_25
from backend.app.agent.prompt_compiler import PromptCompiler
from backend.app.generation_settings import configure


class CharacterReferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name))
        self.visitor, _ = self.store.visitor('')
        self.state = create_demo_state('A campus drama with a heroine and a rival.')
        self.state.status = 'planning_ready'; self.state.current_version_id = 'none'
        self.env = patch.dict(os.environ, {'ARK_API_KEY':'test', 'VIDEO_PROVIDER':'auto', 'SEEDREAM_PRO_API_KEY':'test', 'SEEDREAM_FLASH_API_KEY':'', 'SEEDANCE_25_API_KEY':'test', 'SEEDANCE_20_API_KEY':'', 'MINIMAX_API_KEY':''})
        self.env.start(); self.addCleanup(self.env.stop)
        self.state.creative_spec.source_prompt+=' 精致3D'
        self.people = {'shared_style':{'description':'精致3D，统一比例与材质，精致高级的角色造型','evidence':'精致3D','confidence':.98,'explicit':True},'questions':[], 'required':True, 'characters':[
            {'name':'女主角','appearance':'短发，深蓝校服','reference_names':[]},
            {'name':'对手','appearance':'长发，灰色校服','reference_names':[]}]}

    def prepare(self):
        with patch('backend.app.character_references.request_json',return_value=self.people), patch('backend.app.image_tasks.request_json',return_value={'recommended_model':PRO,'reason':'Detailed consistent turnaround.'}):
            return chars.prepare(self.state,self.store,self.visitor)

    def start(self):
        plan=self.prepare()
        with patch.object(ImageTasks,'_launch') as launch:
            chars.start(self.state,self.store,self.visitor,plan['id'],PRO)
            self.assertEqual(launch.call_count,2)
        return plan

    def complete(self, plan):
        service=ImageTasks(self.store)
        for person in plan['characters']:
            job=service.get(self.visitor,person['task_id'])
            with self.store.connect() as db:
                db.execute('INSERT INTO image_outputs VALUES (?,?,?,?)',(self.visitor,job['run_id'],'image/png',b'\x89PNG\r\n\x1a\nfixture'))
            job.update(status='succeeded',output_url='/api/image-output/'+job['run_id']);service.put(self.visitor,person['task_id'],job)
        chars.sync(self.state,self.store,self.visitor)

    def test_agent_assesses_and_caches_without_keyword_fallback(self):
        plan=self.prepare();self.assertTrue(plan['required']);self.assertEqual(len(plan['characters']),2)
        with patch('backend.app.character_references.request_json') as model:
            self.assertIs(chars.assess(self.state),plan);model.assert_not_called()
        self.state.creative_spec.character_reference_plan={}
        with patch('backend.app.character_references.request_json',return_value={'required':False,'characters':[]}):
            self.assertEqual(chars.assess(self.state)['status'],'skipped')
        with patch.dict(os.environ,{'ARK_API_KEY':''}):
            self.state.creative_spec.character_reference_plan={}
            with self.assertRaises(ValueError):chars.assess(self.state)
        self.assertEqual(self.state.creative_spec.character_reference_plan,{})

    def test_invalid_characters_and_unrelated_reference_are_rejected(self):
        for value in [{'required':True,'characters':[]},{'required':False,'characters':self.people['characters']},
                      {'required':True,'characters':[{'name':'a','appearance':'b','reference_names':['invented.png']}]},
                      {'required':True,'characters':[self.people['characters'][0]]*2}]:
            with self.assertRaises(ValueError):chars.validate_plan(value,[])

    def test_job_is_idempotent_and_recovers_without_resubmission(self):
        plan=self.start()
        with patch.object(ImageTasks,'_launch') as launch:
            chars.start(self.state,self.store,self.visitor,plan['id'],PRO)
            launch.assert_not_called()
            ImageTasks(self.store).recover();chars.sync(self.state,self.store,self.visitor)
            self.assertEqual(plan['status'],'uncertain')
            chars.start(self.state,self.store,self.visitor,plan['id'],PRO);launch.assert_not_called()
        with self.assertRaises(ValueError):chars.ensure_ready(self.state)

    def test_failed_job_and_owner_isolation(self):
        plan=self.start();person=plan['characters'][0];service=ImageTasks(self.store)
        job=service.get(self.visitor,person['task_id']);job['status']='failed';service.put(self.visitor,person['task_id'],job)
        self.assertEqual(service.get('another-visitor',person['task_id']),{})
        self.assertTrue(chars.running(self.state,self.store,self.visitor))
        self.complete(plan)
        self.assertIsNone(service.output('another-visitor',job['run_id']))

    def test_completed_previews_bind_exact_characters_and_provider_inputs(self):
        plan=self.start();self.complete(plan)
        self.assertEqual(plan['status'],'generated')
        self.assertEqual(self.state.creative_spec.multimodal_assets,[])
        with self.assertRaises(ValueError):chars.ensure_ready(self.state)
        previews=[{'character_id':p['id'],'run_id':p['output_url'].rsplit('/',1)[-1], 'data_url':'data:image/jpeg;base64,'+base64.b64encode(b'jpeg-preview').decode()} for p in plan['characters']]
        payload={'plan_id':plan['id'],'previews':previews}
        bad={**payload,'previews':[{**previews[0],'run_id':'other-visitor'},previews[1]]}
        with self.assertRaises(ValueError):chars.attach(self.state,self.store,self.visitor,bad,DemoHandler._normalise_multimodal_assets)
        self.assertEqual(self.state.creative_spec.multimodal_assets,[])
        chars.attach(self.state,self.store,self.visitor,payload,DemoHandler._normalise_multimodal_assets)
        chars.attach(self.state,self.store,self.visitor,payload,DemoHandler._normalise_multimodal_assets)
        chars.ensure_ready(self.state)
        self.assertEqual(len(self.state.creative_spec.multimodal_assets),2)
        self.assertEqual([a['character_name'] for a in self.state.creative_spec.multimodal_assets],['女主角','对手'])
        with patch('backend.app.agent.model_router.request_json',side_effect=ValueError):configure(self.state,{})
        PromptCompiler().compile(self.state)
        body=SeedanceVideoGenerator(SEEDANCE_25)._request_body(self.state)
        self.assertEqual(sum(c['type']=='image_url' for c in body['content']),2)
        self.assertIn('女主角',body['content'][0]['text']);self.assertIn('never render a turnaround sheet',body['content'][0]['text'])
        self.assertEqual(len(self.store.load(self.visitor).creative_spec.character_reference_plan['characters']),2)

    def test_settings_edit_updates_timing_and_default_selection_atomically(self):
        with patch('backend.app.agent.model_router.request_json',side_effect=ValueError):
            result=configure(self.state,{'duration_seconds':12,'aspect_ratio':'9:16','quality_target':'high'})
        self.assertTrue(result['confirmed']);self.assertEqual(result['selected_model'],SEEDANCE_25)
        self.assertEqual(self.state.creative_spec.shots[-1]['end_seconds'],12)
        self.assertEqual(sum(s['duration_seconds'] for s in self.state.creative_spec.shots),12)
        PromptCompiler().compile(self.state)
        self.assertEqual(self.state.creative_spec.generation_contract['output_spec']['aspect_ratio'],'9:16')
        original=self.state.creative_spec
        with self.assertRaises(ValueError):configure(self.state,{'model_id':'unknown','model_changed':True,'duration_seconds':9})
        self.assertIs(self.state.creative_spec,original)
        self.assertEqual(original.generation_spec['duration_seconds'],12)
        for invalid in [True,3,31,6.5]:
            with self.assertRaises(ValueError):configure(self.state,{'duration_seconds':invalid})

    def test_manual_model_choice_persists_until_incompatible_with_new_settings(self):
        from backend.app.agent.model_catalog import SEEDANCE_20
        with patch.dict(os.environ,{'SEEDANCE_20_API_KEY':'test'}), patch('backend.app.agent.model_router.request_json',side_effect=ValueError):
            choice=configure(self.state,{'model_changed':True,'model_id':SEEDANCE_20})
            self.assertEqual(choice['user_model'],SEEDANCE_20)
            choice=configure(self.state,{'aspect_ratio':'9:16'})
            self.assertEqual(choice['selected_model'],SEEDANCE_20)
            choice=configure(self.state,{'duration_seconds':30})
            self.assertEqual(choice['selected_model'],SEEDANCE_25)
            self.assertNotIn('user_model',choice)
            self.assertEqual(self.state.creative_spec.shots[-1]['end_seconds'],30)

    def test_ambiguous_style_asks_once_and_blocks_all_generation(self):
        value={**self.people,'shared_style':{'description':'电影感','evidence':'校园','confidence':.99,'explicit':False}}
        with patch('backend.app.character_references.request_json',return_value=value),patch.object(ImageTasks,'recommend') as recommend:
            plan=chars.prepare(self.state,self.store,self.visitor)
            self.assertEqual(plan['status'],'awaiting_clarification');recommend.assert_not_called()
            self.assertTrue(self.state.autopilot_paused)
            self.assertIn('3D',self.state.conversation[-1]['text'])
            count=len(self.state.conversation);chars.prepare(self.state,self.store,self.visitor)
            self.assertEqual(len(self.state.conversation),count)
        with self.assertRaises(ValueError):chars.start(self.state,self.store,self.visitor,plan['id'],PRO)
        with self.assertRaises(ValueError):chars.ensure_ready(self.state)

    def test_only_one_question_is_sent_until_user_answers(self):
        value={**self.people,'questions':['哪个风格？','马是什么颜色？'],'shared_style':{}}
        with patch('backend.app.character_references.request_json',return_value=value):
            chars.assess(self.state)
        questions=[m for m in self.state.conversation if m.get('kind')=='creative_clarification']
        self.assertEqual(len(questions),1)
        chars.ask(self.state,['另一个问题？'])
        self.assertEqual(len(self.state.conversation),1)

    def test_autonomous_mode_resolves_pending_style_without_questions(self):
        self.state.execution_mode='autopilot';self.state.autopilot_paused=False
        self.state.creative_spec.character_reference_plan={'status':'awaiting_clarification'}
        value={**self.people,'questions':['哪个风格？'],'shared_style':{'description':'电影级写实西部风格','explicit':False,'confidence':.4}}
        with patch('backend.app.character_references.request_json',return_value=value):
            plan=chars.assess(self.state)
        self.assertEqual(plan['status'],'planned');self.assertEqual(plan['questions'],[])
        self.assertFalse(self.state.autopilot_paused)
        self.assertEqual(self.state.conversation,[])
        self.assertEqual(self.state.creative_spec.production_direction['character_visual_style'],'电影级写实西部风格')

    def test_dialogue_answer_locks_one_style_for_all_three_view_prompts(self):
        with patch('backend.app.character_references.request_json',return_value={**self.people,'shared_style':{}}):
            chars.assess(self.state)
        with patch('backend.app.character_references.request_json',return_value=self.people) as decision,patch('backend.app.image_tasks.request_json',return_value={'recommended_model':PRO,'reason':'quality'}):
            chars.answer(self.state,'统一精致3D',self.store,self.visitor)
            self.assertIn('统一精致3D',decision.call_args.args[3]['creative_answers'])
        plan=self.state.creative_spec.character_reference_plan
        self.assertEqual(plan['status'],'planned');self.assertEqual(plan['view_count'],3)
        prompts=[chars.document(self.state.creative_spec,p,'n')['nodes'][0]['text'] for p in plan['characters']]
        for prompt in prompts:
            self.assertFalse(chars.inputs(chars.document(self.state.creative_spec,plan['characters'][0],'n'),'n')['watermark']);self.assertIn('Exactly THREE',prompt);self.assertIn('PURE WHITE',prompt);self.assertIn('NO TEXT',prompt)
            self.assertIn('Sophisticated design',prompt);self.assertTrue(prompt.endswith(plan['shared_style']['description']))
        self.assertEqual(self.state.creative_spec.production_direction['character_visual_style'],plan['shared_style']['description'])

    def test_low_confidence_and_failed_answer_do_not_bypass_dialogue(self):
        value={**self.people,'shared_style':{**self.people['shared_style'],'confidence':.4}}
        with patch('backend.app.character_references.request_json',return_value=value):plan=chars.assess(self.state)
        with patch('backend.app.character_references.request_json',side_effect=ValueError):
            with self.assertRaises(ValueError):chars.answer(self.state,'精致3D',self.store,self.visitor)
        self.assertIs(self.state.creative_spec.character_reference_plan,plan)
        self.assertEqual(plan['status'],'awaiting_clarification')


class CharacterDialogueHTTPTests(unittest.TestCase):
    from tests.test_local_server import LocalServerTests as _helpers
    setUp=_helpers.setUp
    cleanup=_helpers.cleanup
    request=_helpers.request
    session=_helpers.session

    def test_chat_confirmation_persists_message_without_paid_call(self):
        from tests import test_local_server as local
        visitor=self.session();state=create_demo_state('电影级西部故事');state.status='planning_ready'
        state.creative_spec.character_reference_plan={'status':'skipped','required':False}
        self.store.save(visitor,state)
        with patch.object(local.server.VIDEO_GENERATOR,'submit') as paid:
            status,data=self.request('/api/confirm-generation',{'message':'确认生成第一个视频'})
            self.assertEqual(status,200,data);paid.assert_not_called()
        self.assertEqual(data['status'],'awaiting_first_video')
        self.assertEqual(data['conversation'][-2]['text'],'确认生成第一个视频')
        state.creative_spec.character_reference_plan={'status':'awaiting_clarification'}
        import hashlib
        lock=local.server.VISITOR_LOCKS[int(hashlib.sha256(visitor.encode()).hexdigest(),16)%len(local.server.VISITOR_LOCKS)]
        with lock:self.store.save(visitor,state)
        self.assertEqual(self.request('/api/confirm-generation',{'message':'确认'})[0],400)

    def test_answer_updates_main_plan_and_character_style_without_paid_generation(self):
        from backend.app import server
        original=create_demo_state('校园反转剧情')
        original.creative_spec.character_reference_plan={'schema_version':2,'id':'pending','required':True,'status':'awaiting_clarification','questions':['统一什么风格？'],'characters':[]}
        original.conversation=[{'id':'q','role':'assistant','kind':'creative_clarification','text':'统一什么风格？'}]
        visitor=self.session();self.store.save(visitor,original)
        def replan(prompt,*args,**kwargs):return create_demo_state(prompt)
        decision={'required':True,'characters':[{'name':'女主角','appearance':'校服','reference_names':[]}],
                  'questions':[],'shared_style':{'description':'精致3D','evidence':'精致3D','confidence':.98,'explicit':True}}
        with patch.dict(os.environ,{'ARK_API_KEY':'test','SEEDREAM_PRO_API_KEY':'test'}),patch.object(server,'create_enriched_project_state',side_effect=replan),patch('backend.app.character_references.request_json',return_value=decision),patch('backend.app.image_tasks.request_json',return_value={'recommended_model':PRO,'reason':'quality'}),patch.object(ImageTasks,'_launch') as paid:
            status,data=self.request('/api/revise-brief',{'instruction':'精致3D','language':'zh'})
            self.assertEqual(status,200,data);paid.assert_not_called()
        self.assertEqual(data['project_id'],original.project_id)
        self.assertIn('精致3D',data['creative_spec']['source_prompt'])
        self.assertEqual(data['creative_spec']['character_reference_plan']['status'],'planned')
        self.assertEqual(data['creative_spec']['character_reference_plan']['view_count'],3)
        self.assertEqual(data['conversation'][0]['id'],'q')
        self.assertEqual(data['conversation'][-2]['text'],'精致3D')
