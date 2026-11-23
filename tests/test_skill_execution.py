import os
import unittest
from unittest.mock import patch,Mock
from backend.app.demo import create_demo_state,create_enriched_project_state
from backend.app.skill_execution import prepare,quotations,has_prepared_brief,clarify_failure
from backend.app.tools.seedance import SeedanceVideoGenerator
from backend.app.agent.model_catalog import SEEDANCE_25
from backend.app.agent.prompt_compiler import PromptCompiler

class SkillExecutionTests(unittest.TestCase):
    def test_autonomous_preparation_does_not_ask_a_user_to_resolve_creative_choices(self):
        context=self.state.creative_spec.enriched_context
        context['autonomous']=True;context['skill_execution']={'audit':{'missing':['需要自行选择对白优先级']}}
        with patch('backend.app.skill_execution.request_json') as model:
            self.assertFalse(clarify_failure(self.state));model.assert_not_called()
    def test_explicit_permission_to_condense_overrides_literal_dialogue_check(self):
        self.state.creative_spec.enriched_context['resolved_requirements']={'dialogue_edit_policy':'condense_preserve_meaning'}
        result={**self.result,'dialogue_map':[{'source':'我在这里。','text':'我在。'},{'source':'不要离开。','text':'别走。'}],'optimized_prompt':'凯瑟琳说{我在。}；希斯克利夫说{别走。}。'}
        audit={**self.audit,'checks':[{'requirement':'保留原意','evidence':'别走。'}]}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[result,audit]) as model:
            self.assertEqual(prepare(self.state)['status'],'verified')
        self.assertIn('condens',model.call_args.args[3]['inputs']['dialogue_policy'])

    def test_json_format_error_is_not_reported_as_a_missing_user_answer(self):
        import json
        from backend.app.skill_execution import failure_message
        self.assertIn('结构化结果格式异常',failure_message(json.JSONDecodeError('bad','x',0)))
    def test_only_a_real_audited_creative_conflict_becomes_one_question(self):
        self.state.creative_spec.enriched_context['skill_execution']={'audit':{'missing':['完整对白时长与片长冲突']}}
        answer={'requires_user_decision':True,'reason':'完整对白预计超出目标片长。','question':'保留全部对白分段制作，还是保留片长并精简对白？'}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',return_value=answer):
            self.assertTrue(clarify_failure(self.state))
        self.assertEqual(self.state.status,'awaiting_skill_clarification')
        self.assertEqual(self.state.creative_spec.enriched_context['skill_clarification']['question'],answer['question'])
        self.state.creative_spec.enriched_context['skill_execution']={'error_type':'TimeoutError'}
        with patch('backend.app.skill_execution.request_json') as model:
            self.assertFalse(clarify_failure(self.state));model.assert_not_called()

    def setUp(self):
        self.state=create_demo_state('英国卧室，春季，12秒。凯瑟琳说：“我在这里。”希斯克利夫说：“不要离开。”')
        self.state.creative_spec.enriched_context['uploaded_skills']=[{'path':'剧本Skill.md','text':'逐字对白、肌肉级表演、逐秒镜头声音。'}]
        self.result={'applied_skills':['剧本Skill.md'],'execution_steps':[{'step':'解析与表演','result':'全部对白逐字保留，眉毛上移，呼吸收紧。'}],'conflicts':[],
                     'optimized_prompt':'0–6秒 凯瑟琳眉毛上移2毫米，窗光下说{我在这里。}，<衣料摩擦>。6–12秒 希斯克利夫呼吸收紧说{不要离开。}，近景推近。'}
        self.audit={'passed':True,'missing':[],'checks':[{'requirement':'逐字对白','evidence':'我在这里。'}]}

    def test_full_prompt_survives_compiler_and_adapter_and_is_audited(self):
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[self.result,self.audit]) as model:
            result=prepare(self.state);prepare(self.state)
        self.assertEqual(model.call_count,2)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(quotations(self.state.creative_spec.source_prompt),['我在这里。','不要离开。'])
        PromptCompiler().compile(self.state)
        self.assertEqual(self.state.creative_spec.generation_contract['skill_execution']['prompt_sha256'],result['prompt_sha256'])
        body=SeedanceVideoGenerator(SEEDANCE_25,api_key='offline')._request_body(self.state)
        self.assertEqual(body['content'][0]['text'],self.result['optimized_prompt'])
        self.assertIn('skill_hashes',result)
        self.assertTrue(any(h.get('event')=='uploaded_skills_executed' for h in self.state.history))
        self.state.creative_spec.generation_spec['duration_seconds']=20
        with self.assertRaises(ValueError):SeedanceVideoGenerator(SEEDANCE_25,api_key='offline')._request_body(self.state)

    def test_identical_skill_reuploads_are_not_executed_twice_or_removed_from_source(self):
        docs=self.state.creative_spec.enriched_context['uploaded_skills'];docs.append(dict(docs[0]))
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[self.result,self.audit]) as model:
            self.assertEqual(prepare(self.state)['status'],'verified')
        self.assertEqual(len(model.call_args_list[0].args[3]['skills']),1)
        self.assertEqual(len(docs),2)

    def test_missing_dialogue_cannot_pass_even_if_auditor_approves(self):
        bad={**self.result,'optimized_prompt':'两人在卧室说话。'}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[bad,self.audit,bad,self.audit]):
            with self.assertRaises(ValueError):prepare(self.state)
        self.assertEqual(self.state.creative_spec.enriched_context['skill_execution']['status'],'failed')
        with self.assertRaises(ValueError):SeedanceVideoGenerator(SEEDANCE_25,api_key='offline')._request_body(self.state)

    def test_audit_requires_real_evidence_and_cannot_silently_fall_back(self):
        audit={**self.audit,'checks':[{'requirement':'rule','evidence':'invented absent excerpt'}]}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[self.result,audit,self.result,audit]):
            with self.assertRaises(ValueError):prepare(self.state)

    def test_skills_are_executed_and_checked_before_context_and_creative_planning(self):
        order=[];context=Mock();planner=Mock();planner.available.return_value=True
        context.enrich.side_effect=lambda *args:(order.append('context') or {'status':'ready'})
        def plan(prompt,reference,enriched):
            order.append('plan')
            self.assertEqual(enriched['skill_brief']['optimized_prompt'],self.result['optimized_prompt'])
            self.assertEqual(enriched['uploaded_skills'],self.state.creative_spec.enriched_context['uploaded_skills'])
            return create_demo_state(prompt).creative_spec
        planner.plan.side_effect=plan
        def model(*args,**kwargs):
            order.append('skill' if len(order)==0 else 'audit')
            return self.result if len(order)==1 else self.audit
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=model):
            state=create_enriched_project_state(self.state.creative_spec.source_prompt,control_planner=planner,context_agent=context,uploaded_skills=self.state.creative_spec.enriched_context['uploaded_skills'])
        self.assertEqual(order,['skill','audit','context','plan'])
        self.assertEqual(state.status,'planning_ready');self.assertTrue(has_prepared_brief(state))
        state.creative_spec.enriched_context['uploaded_skills'][0]['text']+='新的约束'
        self.assertFalse(has_prepared_brief(state))

    def test_skill_timeout_preserves_inputs_and_never_calls_planner_or_fallback(self):
        context=Mock();planner=Mock()
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=TimeoutError('The read operation timed out')):
            state=create_enriched_project_state(self.state.creative_spec.source_prompt,control_planner=planner,context_agent=context,uploaded_skills=self.state.creative_spec.enriched_context['uploaded_skills'])
        self.assertEqual(state.status,'skill_preparation_failed');self.assertEqual(state.creative_spec.shots,[])
        self.assertEqual(state.creative_spec.source_prompt,self.state.creative_spec.source_prompt)
        self.assertIn('超时',state.creative_spec.planning_notice);self.assertNotIn('timed out',state.creative_spec.planning_notice)
        context.enrich.assert_not_called();planner.plan.assert_not_called()

    def test_planner_failure_after_skill_success_never_creates_prompt_only_recovery_plan(self):
        context=Mock();context.enrich.return_value={'status':'ready'}
        planner=Mock();planner.available.return_value=True;planner.plan.side_effect=ValueError('Invalid plan')
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[self.result,self.audit]):
            state=create_enriched_project_state(self.state.creative_spec.source_prompt,control_planner=planner,context_agent=context,uploaded_skills=self.state.creative_spec.enriched_context['uploaded_skills'])
        self.assertEqual(state.status,'planning_failed');self.assertEqual(state.creative_spec.shots,[])
        self.assertEqual(state.creative_spec.enriched_context['skill_brief']['status'],'verified')

    def test_audit_timeout_can_resume_saved_work_without_reexecuting_skill(self):
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[self.result,TimeoutError('timeout')]):
            with self.assertRaises(ValueError):prepare(self.state)
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',return_value=self.audit) as model:
            self.assertEqual(prepare(self.state)['status'],'verified')
        self.assertEqual(model.call_count,1)

    def test_requested_english_dialogue_overrides_literal_chinese_skill_default(self):
        self.state.creative_spec.source_prompt='全视频对白使用英语。凯瑟琳说：“我在这里。”希斯克利夫说：“不要离开。”'
        result={**self.result,'dialogue_map':[{'source':'我在这里。','text':'I am here.'},{'source':'不要离开。','text':'Do not leave.'}],
                'optimized_prompt':'凯瑟琳说{I am here.}；希斯克利夫说{Do not leave.}。'}
        audit={**self.audit,'checks':[{'requirement':'英语对白','evidence':'I am here.'}]}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.skill_execution.request_json',side_effect=[result,audit]):
            self.assertEqual(prepare(self.state)['status'],'verified')

    def test_planner_cannot_bypass_preparation_or_omit_its_basis_binding(self):
        from backend.app.agent.planner import ArkControlPlanner
        planner=ArkControlPlanner(api_key='offline');context=self.state.creative_spec.enriched_context
        with patch('backend.app.agent.planner.request_json') as request:
            with self.assertRaises(ValueError):planner.plan('film',enriched_context=context)
            request.assert_not_called()
        context['skill_brief']={'status':'verified','prompt_sha256':'prepared-basis'}
        with patch('backend.app.agent.planner.request_json',return_value={}),patch.object(planner,'_to_spec') as convert:
            with self.assertRaises(ValueError):planner.plan('film',enriched_context=context)
            convert.assert_not_called()
