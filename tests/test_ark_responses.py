import json
import unittest
from unittest.mock import patch
from backend.app.tools.ark_responses import DEFAULT_MODEL, request_json, response_text
from backend.app.agent.planner import ArkControlPlanner, ArkRepairPlanner
from backend.app.agent.context import ContextEnrichmentAgent
from backend.app.tools.evaluator import ArkVisionEvaluator
from backend.app.demo import create_demo_state


class Response:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self): return json.dumps(self.value).encode()


def envelope(text):
    return {"status":"completed", "output":[{"type":"reasoning","summary":[{"type":"summary_text","text":"not application JSON"}]}, {"type":"message","content":[{"type":"output_text","text":json.dumps(text)}]}]}


class ArkMigrationTests(unittest.TestCase):
    def test_responses_payload_preserves_role_instructions_and_uses_json_mode(self):
        instruction = "Project-specific acceptance criteria and reference binding rules. Return JSON."
        payload = {"source_prompt":"保留原始需求", "remaining_budget":2}
        with patch('backend.app.tools.ark_responses.urllib.request.urlopen', return_value=Response(envelope({"ok":True}))) as call:
            self.assertEqual(request_json('test-key', DEFAULT_MODEL, instruction, payload, 900), {"ok":True})
        request = call.call_args.args[0]
        body = json.loads(request.data)
        self.assertTrue(request.full_url.endswith('/responses'))
        self.assertEqual(body['model'],DEFAULT_MODEL)
        self.assertEqual(body['input'][0]['content'],instruction)
        self.assertEqual(json.loads(body['input'][1]['content'][0]['text']),payload)
        self.assertEqual(body['text']['format']['type'],'json_object')
        self.assertFalse(body['store'])
        self.assertNotIn('messages',body)
        self.assertNotIn('max_tokens',body)

    def test_reasoning_is_never_parsed_as_the_agents_decision(self):
        self.assertEqual(json.loads(response_text(envelope({'decision':'repair'}))), {'decision':'repair'})
        for status in ('failed','incomplete','cancelled'):
            with self.assertRaises(ValueError): response_text({**envelope({'ok':True}),'status':status})
        with self.assertRaises(ValueError): response_text({'output':[{'type':'reasoning','content':[{'type':'text','text':'untrusted reasoning'}]}]})

    def test_control_planner_keeps_original_policy_prompt_on_new_transport(self):
        planner = ArkControlPlanner(api_key='test-key')
        with patch('backend.app.agent.planner.request_json',side_effect=ValueError('stop after capturing prompt')) as call:
            with self.assertRaises(ValueError): planner.plan('Keep the product logo stable in a 6-second clip.')
        args = call.call_args.args
        self.assertEqual(args[1], DEFAULT_MODEL)
        self.assertIn('acceptance_criteria',args[2])
        self.assertIn('immutable',args[2])
        self.assertIn('4–30',args[2])
        self.assertIn('production_direction',args[2])
        self.assertEqual(args[3]['creative_brief'],'Keep the product logo stable in a 6-second clip.')

    def test_repair_planner_keeps_budget_and_evidence_rules(self):
        state = create_demo_state()
        from backend.app.tools.evaluator import FixtureVisionEvaluator
        report = FixtureVisionEvaluator().analyze(state)
        decision = {'action':'rewrite_prompt','target_shot':'S2','priority':'critical','reason':'failed atomic check','expected_improvement':['camera_control'],'rejected_options':['unbounded retry'],'tool_name':'rewrite_generation_task'}
        with patch('backend.app.agent.planner.request_json',return_value=decision) as call:
            result = ArkRepairPlanner(api_key='test-key').decide(state,report)
        self.assertEqual(result.target_shot,'S2')
        self.assertIn('Never repair an insufficient_evidence',call.call_args.args[2])
        self.assertIn('remaining_budget',call.call_args.args[3])
        self.assertIn('atomic_checks',call.call_args.args[3]['report'])

    def test_plan_retry_receives_the_specific_schema_failure(self):
        planner=ArkControlPlanner(api_key='test-key')
        expected=create_demo_state().creative_spec
        with patch.object(planner,'_request_json',return_value={}) as request, patch.object(planner,'_to_spec',side_effect=[ValueError('production_direction.camera_language must be concise non-empty text'),expected]):
            self.assertIs(planner.plan('A red car stops.'),expected)
        self.assertEqual(request.call_count,2)
        self.assertIn('at most 260 characters',request.call_args_list[0].args[0])
        self.assertIn('production_direction.camera_language must be concise',request.call_args_list[1].args[0])

    def test_reference_pixels_and_reference_governance_are_retained(self):
        content=[{'type':'input_text','text':'Reference R1; only evaluate bound criteria. Return JSON.'}, {'type':'input_image','image_url':'data:image/png;base64,test'}]
        with patch('backend.app.tools.ark_responses.urllib.request.urlopen',return_value=Response(envelope({'ok':True}))) as call:
            request_json('test-key',DEFAULT_MODEL,'JSON only',None,content=content)
        self.assertEqual(json.loads(call.call_args.args[0].data)['input'][1]['content'],content)
        agent=ContextEnrichmentAgent(api_key='test-key', vision_api_key='test-key')
        with patch('backend.app.agent.context.request_json',return_value={'observations':[], 'preserve_features':['red product shell']}) as call:
            result=agent._reference_analysis({'preview_data_url':'data:image/png;base64,test'})
        self.assertEqual(result['preserve_features'],['red product shell'])
        self.assertIn('Only report visible image facts',call.call_args.args[2])

    def test_all_agent_roles_default_to_the_same_new_model(self):
        self.assertEqual(ArkControlPlanner(api_key='test-key').model,DEFAULT_MODEL)
        self.assertEqual(ArkRepairPlanner(api_key='test-key').model,DEFAULT_MODEL)
        self.assertEqual(ArkVisionEvaluator(api_key='test-key').model,DEFAULT_MODEL)
        context=ContextEnrichmentAgent(api_key='test-key')
        self.assertEqual(context.model,DEFAULT_MODEL)
        self.assertEqual(context.vision_model,DEFAULT_MODEL)
