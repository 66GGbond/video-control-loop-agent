import os
import unittest
from unittest.mock import patch
from backend.app.demo import create_demo_state
from backend.app.skill_execution import prepare,quotations
from backend.app.tools.seedance import SeedanceVideoGenerator
from backend.app.agent.model_catalog import SEEDANCE_25
from backend.app.agent.prompt_compiler import PromptCompiler

class SkillExecutionTests(unittest.TestCase):
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
