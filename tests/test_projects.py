import base64
import json
import os
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.app.canvas import empty_canvas
from backend.app.demo import create_empty_state, create_demo_state
from backend.app.image_tasks import ImageTasks
from backend.app.projects import Projects
from backend.app.sessions import SessionStore, encode
from tests import test_local_server as local

PNG=b'\x89PNG\r\n\x1a\nfixture'
DATA='data:image/png;base64,'+base64.b64encode(PNG).decode()


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=SessionStore(Path(self.tmp.name));self.visitor,_=self.store.visitor('');self.projects=Projects(self.store)

    def test_snapshots_keep_independent_states_canvas_and_restore_revision(self):
        first=create_demo_state('校园故事');first.conversation=[{'role':'user','text':'原始消息'}];self.store.save(self.visitor,first)
        doc=empty_canvas();doc['title']='校园画布';doc['nodes']=[{'id':'a','kind':'text','text':'原始画布'}]
        self.store.save_canvas(self.visitor,0,doc)
        second=create_demo_state('茶饮广告');self.store.clear_canvas(self.visitor);self.store.save(self.visitor,second)
        self.assertNotEqual(first.project_id,second.project_id)
        self.assertEqual(len(self.projects.list(self.visitor)['projects']),3)
        revision=self.store.load_canvas(self.visitor)['revision']
        restored,workspace=self.projects.open(self.visitor,first.project_id)
        self.assertEqual(restored.conversation,first.conversation);self.assertEqual(workspace,'vcl')
        self.assertEqual(self.store.load_canvas(self.visitor)['document']['nodes'],[])
        canvas_id=next(p['id'] for p in self.projects.list(self.visitor)['projects'] if p['kind']=='canvas')
        self.projects.open(self.visitor,canvas_id)
        self.assertEqual(self.store.load_canvas(self.visitor)['document'],doc)
        self.assertGreater(self.store.load_canvas(self.visitor)['revision'],revision)
        self.assertIsNone(self.store.save_canvas(self.visitor,revision,empty_canvas()))
        self.assertEqual(self.store.load(self.visitor).project_id,first.project_id)

    def test_canvas_only_draft_is_archived_but_empty_workspaces_are_not(self):
        state=create_empty_state();self.store.save(self.visitor,state)
        self.assertEqual(self.projects.list(self.visitor)['projects'],[])
        doc=empty_canvas();doc['nodes']=[{'id':'a','kind':'image','asset':{'name':'hero.png','preview_data_url':DATA}}]
        self.store.save_canvas(self.visitor,0,doc)
        row=self.projects.list(self.visitor)['projects'][0]
        self.assertEqual(row['workspace'],'canvas');self.assertEqual(row['preview_kind'],'image')
        self.assertEqual(self.projects.image(self.visitor,row['id']),('image/png',PNG))
        self.assertNotIn('canvas',row);self.assertNotIn('summary',row)

    def test_names_are_generated_once_short_and_manual_rename_wins(self):
        state=create_demo_state('校园逆袭剧情');self.store.save(self.visitor,state)
        def decision(*args,**kwargs):
            self.projects.rename(self.visitor,state.project_id,'我的校园剧')
            return {'titles':[{'project_id':state.project_id,'title':'校园逆袭'}]}
        with patch.dict(os.environ,{'ARK_API_KEY':'test'}),patch('backend.app.projects.request_json',side_effect=decision) as model:
            self.projects.name_pending(self.visitor)
            self.projects.name_pending(self.visitor)
            model.assert_called_once()
        self.assertEqual(self.projects.list(self.visitor)['projects'][0]['title'],'我的校园剧')
        self.store.save(self.visitor,state)
        self.assertEqual(self.projects.list(self.visitor)['projects'][0]['title'],'我的校园剧')
        with self.assertRaises(ValueError):self.projects.rename(self.visitor,state.project_id,'  ')

    def test_invalid_agent_title_and_unknown_ids_are_not_accepted(self):
        for title,ident in [('这是一个超过十个汉字的项目名称','same'),('title','someone-else')]:
            state=create_demo_state('新项目');self.store.save(self.visitor,state)
            with patch.dict(os.environ,{'ARK_API_KEY':'test'}),patch('backend.app.projects.request_json',return_value={'titles':[{'project_id':state.project_id if ident=='same' else ident,'title':title}]}):
                self.projects.name_pending(self.visitor)
            row=next(p for p in self.projects.list(self.visitor)['projects'] if p['id']==state.project_id)
            self.assertEqual(row['title_source'],'fallback');self.assertLessEqual(len(row['title']),10)

    def test_background_title_job_does_not_block_open_or_duplicate(self):
        state=create_demo_state('校园剧情');self.store.save(self.visitor,state)
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def decision(*args,**kwargs):
            entered.set();release.wait(3)
            return {'titles':[{'project_id':state.project_id,'title':'校园反转'}]}
        original=self.projects.name_pending
        def work(*args):
            try:return original(*args)
            finally:finished.set()
        with patch.dict(os.environ,{'ARK_API_KEY':'test'}),patch('backend.app.projects.request_json',side_effect=decision) as model,patch.object(self.projects,'name_pending',side_effect=work):
            try:
                self.projects.start_naming(self.visitor);self.assertTrue(entered.wait(1))
                self.projects.start_naming(self.visitor)
                self.projects.open(self.visitor,state.project_id)
                self.assertEqual(model.call_count,1)
            finally:release.set();finished.wait(3)
        self.assertEqual(self.projects.list(self.visitor)['projects'][0]['title'],'校园反转')

    def test_preview_prefers_video_frame_and_ownership_is_enforced(self):
        state=create_demo_state('视频');state.current_video_asset={'poster_data_url':DATA};self.store.save(self.visitor,state)
        self.assertEqual(self.projects.list(self.visitor)['projects'][0]['preview_kind'],'video')
        self.assertEqual(self.projects.image(self.visitor,state.project_id)[1],PNG)
        other,_=self.store.visitor('');self.assertEqual(self.projects.list(other)['projects'],[])
        self.assertIsNone(self.projects.image(other,state.project_id))
        with self.assertRaises(ValueError):self.projects.open(other,state.project_id)
        with self.assertRaises(ValueError):self.projects.rename(other,state.project_id,'other')
        before=self.projects.list(self.visitor)['projects'][0]['updated_at'];self.store.save(self.visitor,state)
        self.assertEqual(self.projects.list(self.visitor)['projects'][0]['updated_at'],before)
        state.execution_mode='autopilot';state.autopilot_paused=False;self.store.save(self.visitor,state)
        self.assertTrue(self.projects.open(self.visitor,state.project_id)[0].autopilot_paused)

    def test_image_tasks_are_isolated_even_when_canvas_node_ids_match(self):
        first=ImageTasks(self.store,'project-one');second=ImageTasks(self.store,'project-two')
        first.put(self.visitor,'draw',{'status':'succeeded'})
        self.assertEqual(second.get(self.visitor,'draw'),{})
        self.assertEqual(first.get(self.visitor,'draw')['status'],'succeeded')

    def test_legacy_project_and_canvas_are_migrated_once(self):
        state=create_demo_state('保留的旧项目');state.project_id='control-plan'
        doc=empty_canvas();doc['nodes']=[{'id':'draw','kind':'image_task','text':'portrait'}, {'id':'out','kind':'output','project_id':'control-plan'}]
        with self.store.connect() as db:
            db.execute('UPDATE visitors SET state=? WHERE id=?',(json.dumps(encode(state)),self.visitor))
            db.execute('INSERT INTO canvases VALUES (?,?,?)',(self.visitor,4,json.dumps(doc)))
        ImageTasks(self.store).put(self.visitor,'draw',{'status':'succeeded'})
        migrated=SessionStore(Path(self.tmp.name));ident=migrated.load(self.visitor).project_id
        self.assertNotEqual(ident,'control-plan');self.assertEqual(len(Projects(migrated).list(self.visitor)['projects']),2)
        self.assertEqual(migrated.load_canvas(self.visitor)['document']['nodes'][1]['project_id'],ident)
        self.assertEqual(ImageTasks(migrated,migrated.canvas_project(self.visitor)).get(self.visitor,'draw')['status'],'succeeded')
        self.assertEqual(SessionStore(Path(self.tmp.name)).load(self.visitor).project_id,ident)

    def test_unscoped_legacy_image_task_moves_once_with_stable_project_id(self):
        state=create_demo_state('保留生图操作');self.store.save(self.visitor,state)
        doc=empty_canvas();doc['nodes']=[{'id':'draw','kind':'image_task','text':'portrait'}]
        self.store.save_canvas(self.visitor,0,doc)
        ImageTasks(self.store).put(self.visitor,'draw',{'status':'recommended'})
        migrated=SessionStore(Path(self.tmp.name))
        self.assertEqual(ImageTasks(migrated,migrated.canvas_project(self.visitor)).get(self.visitor,'draw')['status'],'recommended')
        self.assertEqual(ImageTasks(migrated).get(self.visitor,'draw'),{})
        migrated.clear_canvas(self.visitor)
        other=create_empty_state();migrated.save(self.visitor,other)
        migrated.save_canvas(self.visitor,migrated.load_canvas(self.visitor)['revision'],doc)
        again=SessionStore(Path(self.tmp.name))
        self.assertEqual(ImageTasks(again,other.project_id).get(self.visitor,'draw'),{})


class ProjectHTTPTests(unittest.TestCase):
    setUp=local.LocalServerTests.setUp
    cleanup=local.LocalServerTests.cleanup
    request=local.LocalServerTests.request
    session=local.LocalServerTests.session

    def test_reset_archives_and_open_restores_project_with_rename(self):
        state=create_demo_state('校园故事');state.current_video_asset={'poster_data_url':DATA}
        visitor=self.session();self.store.save(visitor,state)
        _,rows=self.request('/api/projects');self.assertEqual(len(rows['projects']),1)
        self.assertEqual(self.request('/api/projects/rename',{'project_id':state.project_id,'title':'校园逆袭'})[0],200)
        _,blank=self.request('/api/reset',{});self.assertNotEqual(blank['project_id'],state.project_id)
        status,data=self.request('/api/projects/open',{'project_id':state.project_id});self.assertEqual(status,200,data)
        self.assertEqual(self.request('/api/state')[1]['creative_spec']['source_prompt'],'校园故事')
        self.assertEqual(self.request('/api/projects')[1]['projects'][0]['title'],'校园逆袭')
        self.assertEqual(self.request('/api/approve-plan',{},headers={'X-Project-ID':blank['project_id']})[0],409)
        self.cookie=''
        self.assertEqual(self.request('/api/projects')[1]['projects'],[])
        self.assertEqual(self.request('/api/projects/open',{'project_id':state.project_id})[0],400)

    def test_current_project_name_follows_create_rename_and_switch(self):
        _,first=self.request('/api/reset',{'title':'海边旅行'})
        self.assertEqual(first['project_name'],'海边旅行')
        self.request('/api/projects/rename',{'project_id':first['project_id'],'title':'夏日海边'})
        self.assertEqual(self.request('/api/state')[1]['project_name'],'夏日海边')
        _,second=self.request('/api/reset',{'title':'城市漫游'})
        self.assertEqual(second['project_name'],'城市漫游')
        self.request('/api/projects/open',{'project_id':first['project_id']})
        restored=self.request('/api/state')[1]
        self.assertEqual(restored['project_id'],first['project_id'])
        self.assertEqual(restored['project_name'],'夏日海边')
        # Persisting the older domain name must not overwrite the user's rename.
        self.assertEqual(self.request('/api/state')[1]['project_name'],'夏日海边')

    def test_revisions_stay_in_same_project_and_old_project_can_be_continued(self):
        def plan(prompt,*args,**kwargs):
            state=create_demo_state(prompt);state.status='planning_ready';return state
        with patch.object(local.server,'create_enriched_project_state',side_effect=plan):
            _,first=self.request('/api/create-project',{'prompt':'校园剧情'})
            _,revised=self.request('/api/revise-brief',{'instruction':'加一个反转'})
            self.assertEqual(first['project_id'],revised['project_id'])
            self.assertEqual(len(self.request('/api/projects')[1]['projects']),1)
            self.request('/api/reset',{})
            _,second=self.request('/api/create-project',{'prompt':'茶饮广告'})
            self.assertNotEqual(first['project_id'],second['project_id'])
            self.request('/api/projects/open',{'project_id':first['project_id']})
            _,continued=self.request('/api/revise-brief',{'instruction':'改为黄昏'})
        self.assertEqual(continued['project_id'],first['project_id'])
        self.assertIn('改为黄昏',continued['creative_spec']['source_prompt'])
        self.assertEqual(len(self.request('/api/projects')[1]['projects']),2)

    def test_navigation_does_not_change_project_type(self):
        visitor=self.session();state=create_demo_state('茶饮视频');self.store.save(visitor,state)
        self.assertEqual(self.request('/api/projects/workspace',{'workspace':'canvas'})[0],200)
        state.creative_spec.max_iterations=3;self.store.save(visitor,state)
        self.assertEqual(self.request('/api/projects/open',{'project_id':state.project_id})[1]['workspace'],'vcl')
        self.assertEqual(self.request('/api/projects/workspace',{'workspace':'unexpected'})[0],400)

    def test_named_vcl_creation_keeps_canvas_independent(self):
        visitor=self.session();state=create_demo_state('原来的VCL项目');self.store.save(visitor,state)
        doc=empty_canvas();doc['title']='独立画布';doc['nodes']=[{'id':'n','kind':'text','text':'画布内容','title':'','x':0,'y':0}]
        current=self.request('/api/canvas')[1];self.request('/api/canvas',{'revision':current['revision'],'document':doc})
        canvas_id=self.request('/api/canvas')[1]['project_id']
        _,new=self.request('/api/reset',{'title':'我的新故事'})
        self.assertEqual(new['project_name'],'我的新故事')
        self.assertEqual(self.request('/api/canvas')[1]['document'],doc)
        self.assertEqual(self.request('/api/canvas')[1]['project_id'],canvas_id)
        rows=self.request('/api/projects')[1]['projects']
        self.assertEqual({p['kind'] for p in rows},{'vcl','canvas'})
        self.assertEqual(next(p['title'] for p in rows if p['id']==new['project_id']),'我的新故事')
        self.request('/api/projects/open',{'project_id':canvas_id})
        self.assertEqual(self.request('/api/state')[1]['project_id'],new['project_id'])
        self.request('/api/projects/open',{'project_id':state.project_id})
        self.assertEqual(self.request('/api/canvas')[1]['project_id'],canvas_id)

    def test_new_canvas_does_not_replace_vcl_and_rejects_stale_canvas(self):
        visitor=self.session();state=create_demo_state('VCL保留');self.store.save(visitor,state)
        old=self.request('/api/canvas')[1]['project_id']
        status,data=self.request('/api/new-canvas',{'title':'新画布'})
        self.assertEqual(status,200,data);self.assertNotEqual(data['project_id'],old)
        self.assertEqual(self.request('/api/state')[1]['project_id'],state.project_id)
        self.assertEqual(self.request('/api/canvas')[1]['document']['title'],'新画布')
        self.assertEqual(self.request('/api/generate-image',{'node_id':'n'},headers={'X-Canvas-ID':old})[0],409)
