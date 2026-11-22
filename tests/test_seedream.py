import base64
import json
import os
import unittest
from unittest.mock import patch
from backend.app.tools import seedream
from backend.app.image_tasks import ImageTasks,inputs
from backend.app.canvas import empty_canvas
from backend.app import server
from tests import test_local_server as local

PNG=b'\x89PNG\r\n\x1a\nfixture'

def document():
    doc=empty_canvas();doc['nodes']=[{'id':'draw','kind':'image_task','text':'A detailed red tea cup','title':'Image','x':0,'y':0}];return doc

class Response:
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,*args):return json.dumps({'data':[{'b64_json':base64.b64encode(PNG).decode()}]}).encode()

class SeedreamTests(unittest.TestCase):
    def test_adapter_keeps_model_key_and_single_image_parameters(self):
        with patch.dict(os.environ,{'SEEDREAM_FLASH_API_KEY':'flash-key'}),patch('backend.app.tools.seedream.urllib.request.urlopen',return_value=Response()) as call:
            self.assertEqual(seedream.generate(seedream.FLASH,'Keep @cup.png',['data:image/png;base64,AA==']),PNG)
        request=call.call_args.args[0];body=json.loads(request.data)
        self.assertEqual(request.get_header('Authorization'),'Bearer flash-key')
        self.assertEqual(body['model'],seedream.FLASH);self.assertEqual(body['size'],'2K');self.assertTrue(body['watermark'])
        self.assertEqual(body['response_format'],'b64_json');self.assertNotIn('sequential_image_generation',body)
        self.assertEqual(body['image'],['data:image/png;base64,AA=='])

    def test_unresolved_tasks_and_incompatible_media_are_not_silently_ignored(self):
        for kind in ('audio','video','task','output','image_task'):
            doc=document();doc['nodes'].append({'id':'ref','kind':kind,'text':'other task'});doc['edges']=[{'source':'ref','target':'draw'}]
            with self.subTest(kind=kind),self.assertRaises(ValueError):inputs(doc,'draw')

    def test_generated_images_form_an_artifact_boundary(self):
        doc=document();doc['nodes'] += [{'id':'old','kind':'image_task','text':'Do not leak this old image prompt'}, {'id':'ref','kind':'image','image_run_id':'a'*32,'asset':{'name':'cup.png','preview_data_url':'data:image/png;base64,AA=='}}]
        doc['edges']=[{'source':'old','target':'ref'},{'source':'ref','target':'draw'}]
        result=inputs(doc,'draw');self.assertEqual(result['prompt'],'A detailed red tea cup');self.assertEqual(len(result['images']),1)

class ImageHTTPTests(unittest.TestCase):
    cleanup=local.LocalServerTests.cleanup
    request=local.LocalServerTests.request
    session=local.LocalServerTests.session
    def setUp(self):
        local.LocalServerTests.setUp(self);self.env=patch.dict(os.environ,{'SEEDREAM_PRO_API_KEY':'pro','SEEDREAM_FLASH_API_KEY':'flash','ARK_API_KEY':'decision'})
        self.env.start();self.addCleanup(self.env.stop);self.visitor=self.session();self.store.save_canvas(self.visitor,0,document())

    def recommend(self):
        with patch('backend.app.image_tasks.request_json',return_value={'recommended_model':seedream.PRO,'reason':'Detailed product still','tradeoffs':[],'alternatives':[{'model_id':seedream.FLASH,'reason':'Latency priority'}]}):
            status,data=self.request('/api/recommend-image-model',{'node_id':'draw'})
        self.assertEqual(status,200,data);return data

    def test_decision_is_not_generation_and_forged_selection_is_rejected(self):
        with patch('backend.app.image_tasks.seedream.generate') as generate:
            choice=self.recommend();generate.assert_not_called()
            self.assertEqual(self.request('/api/generate-image',{'node_id':'draw','choice_id':choice['choice_id'],'model_id':'forged'})[0],400)
            self.assertEqual(self.request('/api/generate-image',{'node_id':'draw','choice_id':choice['choice_id'],'model_id':[]})[0],400)
        self.assertNotIn('pro-key',str(choice));self.assertNotIn('key_env',str(choice))

    def test_confirmed_model_is_bound_and_duplicate_submission_is_idempotent(self):
        choice=self.recommend();payload={'node_id':'draw','choice_id':choice['choice_id'],'model_id':seedream.FLASH}
        with patch.object(ImageTasks,'_launch') as start:
            self.assertEqual(self.request('/api/generate-image',payload)[0],202)
            self.assertEqual(self.request('/api/generate-image',payload)[0],202);start.assert_called_once()
        result=self.request('/api/image-task/draw')[1];self.assertEqual(result['selected_model'],seedream.FLASH)

    def test_changed_inputs_cannot_generate_with_stale_confirmation(self):
        choice=self.recommend();doc=document();doc['nodes'][0]['text']='New requirement';self.store.save_canvas(self.visitor,1,doc)
        with patch('backend.app.image_tasks.seedream.generate') as generate:
            self.assertEqual(self.request('/api/generate-image',{'node_id':'draw','choice_id':choice['choice_id'],'model_id':seedream.PRO})[0],400);generate.assert_not_called()

    def test_invalid_llm_recommendation_is_labeled_as_policy_fallback(self):
        with patch('backend.app.image_tasks.request_json',return_value={'recommended_model':'invented','reason':'claim'}):
            result=self.request('/api/recommend-image-model',{'node_id':'draw'})[1]
        self.assertEqual(result['source'],'policy_fallback');self.assertIn(result['recommended_model'],seedream.KEYS)

    def test_successful_result_persists_after_project_reset_and_is_session_isolated(self):
        choice=self.recommend();project=self.store.load(self.visitor).project_id;service=ImageTasks(self.store,project);result={**choice,'run_id':choice['choice_id'],'selected_model':seedream.PRO,'status':'running'}
        with patch('backend.app.image_tasks.seedream.generate',return_value=PNG):service._work(self.visitor,'draw',result,inputs(document(),'draw'))
        self.request('/api/reset',{});self.assertEqual(self.request('/api/image-task/draw')[1],{})
        self.request('/api/projects/open',{'project_id':project});saved=self.request('/api/image-task/draw')[1]
        self.assertEqual(saved['status'],'succeeded');self.assertEqual(service.output(self.visitor,saved['run_id'])[1],PNG)
        self.assertIsNone(service.output('different-session',saved['run_id']))
        self.assertEqual(len(self.request('/api/image-results')[1]['results']),1)

    def test_recovery_marks_interrupted_work_uncertain_without_resubmission(self):
        choice=self.recommend();service=ImageTasks(self.store);service.put(self.visitor,'draw',{**choice,'status':'running'})
        with patch('backend.app.image_tasks.seedream.generate') as generate:
            service.recover();generate.assert_not_called()
        self.assertEqual(service.get(self.visitor,'draw')['status'],'uncertain')
