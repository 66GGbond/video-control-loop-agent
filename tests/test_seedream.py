import base64
import json
import os
import unittest
from unittest.mock import patch
from backend.app.tools import seedream
from backend.app.image_tasks import ImageTasks,inputs
from backend.app import server
from tests import test_local_server as local

PNG=b'\x89PNG\r\n\x1a\nfixture'

def document():
    doc={'nodes':[],'edges':[]};doc['nodes']=[{'id':'draw','kind':'image_task','text':'A detailed red tea cup','title':'Image','x':0,'y':0}];return doc

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
