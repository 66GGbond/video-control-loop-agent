import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from backend.app.agent.context import ContextEnrichmentAgent
from backend.app.agent.planner import ArkControlPlanner
from backend.app.demo import create_demo_state,create_empty_state,create_enriched_project_state
from backend.app.sessions import SessionStore,encode
from backend.app.projects import Projects
from backend.app.chat_turns import decide,pending_question
from backend.app.tools.generator import ArkVideoGenerator

class DirectPlanningTests(unittest.TestCase):
    def test_one_planning_request_uses_skills_and_decisions_and_supplies_provider_prompt(self):
        fixture=create_demo_state('15秒英国爱情故事，英语对白').creative_spec
        parsed={'objective':fixture.goal,'immutable_constraints':fixture.immutable_character_attributes,
                'shots':[{**shot, 'beat_function': 'new_editorial_label', 'pacing': 'slow'} for shot in fixture.shots],
                'risk_forecast':fixture.risk_forecast,
                'evidence_sampling':fixture.evidence_sampling,
                'generation_spec':{**fixture.generation_spec, 'duration_seconds': 20},
                'recommended_iterations':1,'planning_rationale':'关注人物动作和情感',
                'acceptance_criteria':fixture.acceptance_criteria,
                'video_prompt':'英国乡村厨房。Catherine says: "I am Heathcliff." Warm firelight; hold on her conflicted reaction.',
                'skill_application':[{'step':'表演','result':'炉火侧光下压低声音，目光回避'}]}
        skills=[{'path':'SKILL.md','text':'对白附上角色的身体表演、目光和光线。'}]
        answers=[{'question':'是否精简','answer':'保留原意精简英语对白'}]
        planner=ArkControlPlanner(api_key='offline')
        with patch.object(planner,'_request_json',return_value=parsed) as llm,patch.object(ContextEnrichmentAgent,'_request_json') as context_call:
            state=create_enriched_project_state(fixture.source_prompt,control_planner=planner,uploaded_skills=skills,creative_answers=answers,resolved_requirements={'character_reference_policy':'skip'})
        self.assertEqual(state.status,'planning_ready');llm.assert_called_once();context_call.assert_not_called()
        self.assertEqual(state.creative_spec.generation_spec['duration_seconds'], 15)
        self.assertEqual([shot['intent'] for shot in state.creative_spec.shots],
                         [shot['intent'] for shot in fixture.shots])
        payload=llm.call_args.args[1]
        self.assertEqual(payload['user_imported_skills'],skills)
        self.assertEqual(payload['enriched_context']['creative_answers'],answers)
        self.assertEqual(state.creative_spec.enriched_context['skill_application'],parsed['skill_application'])
        prompt=ArkVideoGenerator._generation_prompt(state,None)
        self.assertIn(parsed['video_prompt'],prompt)
        self.assertIn('Current output settings',prompt)
        self.assertNotIn('skill_execution',state.creative_spec.generation_contract)
        without_explicit_duration = planner._to_spec('英国爱情故事，英语对白', None, parsed)
        self.assertEqual(without_explicit_duration.generation_spec['duration_seconds'], 20)
        self.assertEqual(without_explicit_duration.shots[-1]['end_seconds'], 20)

    def test_source_labels_and_six_sampling_focuses_are_not_execution_gates(self):
        from backend.app.agent.sampling import validate_evidence_sampling_policy
        planner=ArkControlPlanner(api_key='offline')
        ids={f'S{i}' for i in range(1,7)}
        policy={'focus_shot_ids':sorted(ids),'critical_moments':[{'shot_id':i,'reason':'观察动作完成'} for i in sorted(ids)],'rationale':'逐一观察所有计划镜头'}
        self.assertEqual(len(validate_evidence_sampling_policy(policy,allowed_shot_ids=ids)['focus_shot_ids']),6)
        criteria=[{'requirement_id':'r'+i,'shot_id':i,'source':source,'requirement':'人物完成动作','expected':'可见完成状态','critical':True} for i,source in zip(sorted(ids),['creative_brief','user_instruction','condensed_dialogue','uploaded_skill','prompt','control_plan'])]
        self.assertEqual(len(planner._acceptance_criteria(criteria,ids)),6)
        direction=planner._production_direction({'camera_language':'长镜头'+('保留表演细节。'*80),'visual_treatment':'炉火侧光','motion_continuity':'保持动作连续','negative_guards':['避免手部穿模']},[])
        self.assertGreater(len(direction['camera_language']),260)

    def test_model_selection_reuses_main_agent_decision_without_another_model_call(self):
        from backend.app.agent.model_router import recommend
        from backend.app.agent.model_catalog import SEEDANCE_25
        state=create_demo_state('英国爱情故事');state.creative_spec.enriched_context.update(video_prompt='完整提示词',video_model_recommendation={'model_id':SEEDANCE_25,'reason':'主 Agent 基于需求选择'})
        with patch.dict(os.environ,{'SEEDANCE_25_API_KEY':'offline','VIDEO_PROVIDER':'auto'}),patch('backend.app.agent.model_router.request_json') as llm:
            choice=recommend(state)
        llm.assert_not_called();self.assertEqual(choice['recommended_model'],SEEDANCE_25)
        self.assertEqual(choice['source'],'doubao_seed_2_1_pro')

    def test_initial_brief_with_policy_and_answer_without_exact_ids_are_accepted(self):
        initial=create_empty_state();message='英国场景，15秒，英语对白，跳过人物参考图'
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.chat_turns.request_json',return_value={'intent':'brief','character_reference_policy':'skip'}):
            decision=decide(initial,message)
        self.assertEqual(decision['intent'],'brief');self.assertEqual(decision['character_reference_policy'],'skip')
        initial.status='awaiting_skill_clarification';initial.creative_spec.enriched_context['skill_clarification']={'question':'精简对白吗？'}
        with patch.dict(os.environ,{'ARK_API_KEY':'offline'}),patch('backend.app.chat_turns.request_json',return_value={'intent':'answer','question_id':'model-echoed-wrong-id','summary':'保留原意精简对白'}):
            answer=decide(initial,'可以，保留原意')
        self.assertEqual(answer['question_id'],pending_question(initial)['id'])

    def test_editor_retirement_backs_up_and_preserves_all_vcl_archives(self):
        with TemporaryDirectory() as tmp:
            store=SessionStore(Path(tmp));visitor,_=store.visitor('');state=create_demo_state('保留的历史项目');store.save(visitor,state)
            with store.connect() as db:
                db.execute("ALTER TABLE projects ADD COLUMN canvas TEXT NOT NULL DEFAULT '{}'")
                db.execute('CREATE TABLE canvases(visitor_id TEXT,revision INTEGER,document TEXT)')
                db.execute('CREATE TABLE canvas_projects(visitor_id TEXT,id TEXT)')
                db.execute("INSERT INTO canvases VALUES (?,1,?)",(visitor,'{"nodes":[{"text":"old board"}]}'))
                db.execute("INSERT INTO projects SELECT visitor_id,'old-board','board',title_source,state,'canvas',summary,preview_mime,preview,preview_kind,created_at,updated_at,'canvas',canvas FROM projects WHERE id=?",(state.project_id,))
            restored=SessionStore(Path(tmp))
            self.assertEqual([p['id'] for p in Projects(restored).list(visitor)['projects']],[state.project_id])
            self.assertEqual(restored.load(visitor).creative_spec.source_prompt,state.creative_spec.source_prompt)
            with restored.connect() as db:
                self.assertNotIn('canvas',{r[1] for r in db.execute('PRAGMA table_info(projects)')})
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='canvases'").fetchone())
            with sqlite3.connect(Path(tmp)/'backups/before-editor-removal.sqlite3') as backup:
                self.assertEqual(backup.execute('SELECT count(*) FROM projects').fetchone()[0],2)
