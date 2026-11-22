import copy
import unittest
from tests import test_local_server as local
from backend.app.canvas import empty_canvas, validate_canvas
from backend.app.server import DemoHandler
from backend.app.demo import create_demo_state


def node(ident, kind='text'):
    return {'id':ident,'kind':kind,'x':10,'y':20,'title':'A node','text':'A 6 second film'}


class CanvasValidationTests(unittest.TestCase):
    def validate(self, doc):
        return validate_canvas(doc, DemoHandler._normalise_multimodal_assets)

    def test_rejects_cycles_dangling_edges_duplicate_ids_and_nonfinite_coordinates(self):
        base=empty_canvas();base['nodes']=[node('a'),node('b')]
        variants=[]
        for edges in ([{'source':'a','target':'b'},{'source':'b','target':'a'}],[{'source':'missing','target':'a'}]):
            doc=copy.deepcopy(base);doc['edges']=edges;variants.append(doc)
        doc=copy.deepcopy(base);doc['nodes'][1]['id']='a';variants.append(doc)
        for value in (float('inf'),float('nan'),True):
            doc=copy.deepcopy(base);doc['nodes'][0]['x']=value;variants.append(doc)
        for doc in variants:
            with self.subTest(doc=doc),self.assertRaises(ValueError):self.validate(doc)

    def test_media_roundtrip_reuses_agent_validation_and_does_not_allow_executable_urls(self):
        doc=empty_canvas();image=node('image','image')
        image['asset']={'name':'hero.png','mime_type':'image/png','preview_data_url':'data:image/png;base64,AA=='}
        doc['nodes']=[image];result=self.validate(doc)
        self.assertEqual(self.validate(result),result)
        video=node('video','video');video['asset']={'name':'v.mp4','mime_type':'video/mp4','url':'javascript:alert(1)','duration_seconds':5}
        doc['nodes']=[video]
        with self.assertRaises(ValueError):self.validate(doc)

    def test_strips_unrecognized_fields_and_rejects_invalid_node_kinds(self):
        doc=empty_canvas();doc['nodes']=[{**node('a'),'api_key':'never store arbitrary fields'}]
        self.assertNotIn('api_key',str(self.validate(doc)))
        doc['nodes'][0]['kind']='image_generator'
        with self.assertRaises(ValueError):self.validate(doc)


class CanvasHTTPTests(unittest.TestCase):
    cleanup=local.LocalServerTests.cleanup
    request=local.LocalServerTests.request
    session=local.LocalServerTests.session
    def setUp(self):local.LocalServerTests.setUp(self)

    def test_persistence_isolated_sessions_and_stale_writes_cannot_replace_saved_work(self):
        self.assertEqual(self.request('/api/canvas')[1]['revision'],0)
        doc=empty_canvas();doc['nodes']=[node('a')]
        self.assertEqual(self.request('/api/canvas',{'revision':0,'document':doc})[0],200)
        first_cookie=self.cookie
        status,saved=self.request('/api/canvas');self.assertEqual(status,200);self.assertEqual(saved['document'],doc)
        self.assertEqual(self.request('/api/canvas',{'revision':0,'document':empty_canvas()})[0],409)
        self.assertEqual(self.request('/api/canvas')[1]['document'],doc)
        self.cookie='';self.assertEqual(self.request('/api/canvas')[1]['document']['nodes'],[])
        self.cookie=first_cookie;self.assertEqual(self.request('/api/canvas')[1]['document'],doc)

    def test_canvas_survives_project_reset_and_does_not_submit_models(self):
        from unittest.mock import patch
        doc=empty_canvas();doc['nodes']=[node('a')]
        with patch.object(local.server.VIDEO_GENERATOR,'submit') as submit:
            self.request('/api/canvas',{'revision':0,'document':doc});self.request('/api/reset',{})
            self.assertEqual(self.request('/api/canvas')[1]['document'],doc)
            submit.assert_not_called()

    def test_project_reference_explicit_read_has_project_identity_and_no_filesystem_fields(self):
        visitor=self.session();state=create_demo_state()
        state.creative_spec.multimodal_assets=[{'name':'hero.png','media_type':'image','mime_type':'image/png','preview_data_url':'data:image/png;base64,AA==','local_video_path':'private'}]
        self.store.save(visitor,state)
        status,data=self.request('/api/canvas-reference',{'project_id':state.project_id,'index':0})
        self.assertEqual(status,200,data);self.assertIn('preview_data_url',data['asset']);self.assertNotIn('local_video_path',data['asset'])
        self.assertEqual(self.request('/api/canvas-reference',{'project_id':'old-project','index':0})[0],400)
        self.assertEqual(self.request('/api/canvas-reference',{'project_id':state.project_id,'index':-1})[0],400)

    def test_canvas_endpoints_keep_same_origin_and_schema_guards(self):
        doc=empty_canvas()
        self.assertEqual(self.request('/api/canvas',{'revision':0,'document':doc},headers={'Origin':'https://example.com'})[0],403)
        self.assertEqual(self.request('/api/canvas',{'revision':0,'document':{'schema':999}})[0],400)
