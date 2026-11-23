import base64
import hashlib
import http.client
import json
import os
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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




class ProjectHTTPTests(unittest.TestCase):
    setUp=local.LocalServerTests.setUp
    cleanup=local.LocalServerTests.cleanup
    request=local.LocalServerTests.request
    session=local.LocalServerTests.session

    def test_history_and_previews_remain_readable_during_a_locked_model_request(self):
        visitor=self.session();state=create_demo_state('历史中的项目')
        state.current_video_asset={'poster_data_url':DATA}
        self.store.save(visitor,state)
        lock=local.server.VISITOR_LOCKS[int(hashlib.sha256(visitor.encode()).hexdigest(),16)%len(local.server.VISITOR_LOCKS)]
        # A slow planner/Skill holds this same lock on its request thread.
        # History must use the committed archive without waiting or saving state.
        with lock,patch.object(self.store,'save',side_effect=AssertionError('History must be read-only')):
            for path in ['/api/projects','/api/project-preview/'+state.project_id]:
                conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=1)
                try:
                    conn.request('GET',path,headers={'Cookie':self.cookie})
                    response=conn.getresponse();body=response.read()
                    self.assertEqual(response.status,200)
                    if path=='/api/projects':self.assertEqual(json.loads(body)['projects'][0]['id'],state.project_id)
                    else:self.assertEqual(body,PNG)
                finally:conn.close()
        self.assertEqual(self.store.load(visitor).project_id,state.project_id)

    def test_nonblocking_history_still_checks_host_and_owner(self):
        visitor=self.session();state=create_demo_state('私有项目')
        self.store.save(visitor,state)
        self.assertEqual(self.request('/api/projects',headers={'Host':'untrusted.example'})[0],403)
        self.cookie=''
        self.assertEqual(self.request('/api/projects')[1]['projects'],[])
        self.assertEqual(self.request('/api/project-preview/'+state.project_id)[0],404)

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
