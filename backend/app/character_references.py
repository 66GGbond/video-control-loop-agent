"""Agent-planned character turnarounds using the durable image-task service."""
import json
import os
import uuid
from pathlib import Path

from .image_tasks import ImageTasks, inputs, signature, cards
from .tools.ark_responses import request_json, configured_model
from .agent.asset_manifest import build_asset_manifest

SKILL = Path(__file__).parent / 'agent/skills/character_references.md'
EDITABLE = {'planning_ready', 'awaiting_first_video', 'awaiting_repair_upload'}


def validate_plan(value, assets):
    if not isinstance(value, dict) or not isinstance(value.get('required'), bool):
        raise ValueError('人物参考规划格式无效，请重试。')
    people = value.get('characters')
    if not isinstance(people, list) or (value['required'] and not 1 <= len(people) <= 6) or (not value['required'] and people):
        raise ValueError('人物参考规划需要列出重要人物。')
    names = {a['name'] for a in assets if a.get('media_type') == 'image'}
    result = []
    for person in people:
        if not isinstance(person, dict) or any(not isinstance(person.get(k), str) or not person[k].strip() or len(person[k]) > limit for k, limit in [('name', 100), ('appearance', 2000)]):
            raise ValueError('人物参考规划缺少人物或外观描述。')
        refs = person.get('reference_names', [])
        if not isinstance(refs, list) or any(not isinstance(n, str) or n not in names for n in refs) or len(refs) > 10:
            raise ValueError('人物参考素材关联无效。')
        if person['name'].strip().casefold() in {p['name'].casefold() for p in result}:
            raise ValueError('人物参考规划包含重复人物。')
        result.append({'id': 'C' + str(len(result) + 1), 'name': person['name'].strip(), 'appearance': person['appearance'].strip(), 'reference_names': list(dict.fromkeys(refs))})
    questions = value.get('questions', [])
    if not isinstance(questions,list) or len(questions)>3 or any(not isinstance(q,str) or not q.strip() or len(q)>500 for q in questions):
        raise ValueError('创意确认问题格式无效。')
    style = value.get('shared_style', {})
    if not isinstance(style,dict):
        raise ValueError('统一风格格式无效。')
    return {'required': value['required'], 'characters': result, 'shared_style': style, 'questions': list(dict.fromkeys(q.strip() for q in questions))}


def source_assets(spec):
    return [a for a in (spec.multimodal_assets or ([spec.reference_asset] if spec.reference_asset else [])) if not a.get('generated_character_plan')]


def source_reference(spec):
    return next((a for a in source_assets(spec) if a.get('media_type') == 'image' or a.get('mime_type', '').startswith('image/')), None)


def assess(state):
    spec = state.creative_spec
    if state.status not in EDITABLE or not spec.shots:
        raise ValueError('请先生成脚本，再规划人物参考。')
    autonomous = state.execution_mode == 'autopilot' and not state.autopilot_paused
    if autonomous and spec.character_reference_plan.get('status')=='awaiting_clarification':
        spec.character_reference_plan={}
    if spec.character_reference_plan:
        old=spec.character_reference_plan
        if old.get('schema_version')==2 or old.get('status') not in {'planned'}:
            return old
        spec.character_reference_plan={}
    if not os.getenv('ARK_API_KEY'):
        raise ValueError('人物参考规划需要配置决策模型。')
    language = 'English' if spec.enriched_context.get('ui_language') == 'en' else 'Simplified Chinese'
    decision = request_json(os.environ['ARK_API_KEY'], configured_model(), SKILL.read_text() + '\nInterface language: ' + language + ('\nAutonomous mode: make all creative decisions yourself. Infer one coherent visual style from the brief and storyboard; return a concrete shared_style.description even when not explicit. Do not ask questions.' if autonomous else '\nAsk at most ONE essential question at a time; do not ask about optional details. Never repeat questions already answered in creative_answers.'),
                            {'brief': spec.source_prompt, 'storyboard': spec.shots, 'visual_treatment': spec.production_direction,
                             'creative_answers': spec.enriched_context.get('creative_answers',[]),
                             'planner_questions':spec.enriched_context.get('creative_questions',[]),
                             'uploaded_images': [{'name': a['name'], 'role': a.get('role')} for a in source_assets(spec) if a.get('media_type') == 'image']}, 3000, timeout=90)
    plan = validate_plan(decision, source_assets(spec))
    plan['questions']=list(dict.fromkeys([*plan['questions'],*spec.enriched_context.get('creative_questions',[])]))[:3]
    style=plan['shared_style']
    evidence=style.get('evidence','')
    confidence=style.get('confidence',0)
    user_text=spec.source_prompt+'\n'+'\n'.join(a.get('answer','') if isinstance(a,dict) else str(a) for a in spec.enriched_context.get('creative_answers',[]))
    style_clear=(style.get('explicit') is True and isinstance(style.get('description'),str) and 0<len(style['description'].strip())<=1500
                 and isinstance(evidence,str) and bool(evidence.strip()) and evidence.strip() in user_text
                 and isinstance(confidence,(int,float)) and not isinstance(confidence,bool) and .85<=confidence<=1)
    if autonomous:
        plan['questions']=[]
        style_clear=bool(style.get('description','').strip())
        if plan['required'] and not style_clear:
            raise ValueError('自主规划暂未返回统一画面风格，请重试。')
    if plan['required'] and not style_clear:
        question=('What visual style should this whole video share: photorealistic live action, refined 3D, 2D animation, or another style?'
                  if language=='English' else '这支视频的所有人物统一采用哪种画面风格？例如超写实仿真人、精致 3D、二维动画，或你喜欢的其他风格。')
        plan['questions']=list(dict.fromkeys([question,*plan['questions']]))[:3]
    plan.update(id=uuid.uuid4().hex, schema_version=2, view_count=3,
                status='awaiting_clarification' if plan['questions'] else 'planned' if plan['required'] else 'skipped')
    if plan['questions']:
        state.autopilot_paused=True
        ask(state,plan['questions'][:1])
    elif plan['required']:
        spec.production_direction['character_visual_style']=style['description'].strip()
        spec.generation_spec['quality_target']='high'
        spec.generation_contract={};spec.evaluation_contract={}
        spec.video_model_selection['confirmed']=False
    spec.character_reference_plan = plan
    if not autonomous and not plan['questions']:
        from datetime import datetime, timezone
        state.conversation.append({'id':uuid.uuid4().hex,'at':datetime.now(timezone.utc).isoformat(),'role':'assistant','kind':'generation_confirmation','text':('The plan is ready. Shall I generate the first video?' if language=='English' else '当前方案已基本确定，是否开始生成第一个视频？')})
    return plan


def document(spec, person, node):
    style=spec.character_reference_plan.get('shared_style',{}).get('description','')
    prompt = ('Create a premium character design turnaround sheet for ONE important character in ONE shared video world. '
              'Exactly THREE equal full-body views, left to right: front, side profile, back. '
              'The same identity, proportions, face, hairstyle, wardrobe and materials in every view. '
              'Seamless PURE WHITE background (#FFFFFF), refined even studio lighting, no environment or props beyond required wardrobe. '
              'NO TEXT, letters, numbers, captions, view labels, logos, signatures or extra figures. '
              'Sophisticated design, intentional silhouette, exquisite grooming, refined wardrobe detailing, premium materials and polished presentation. '
              'Use supplied character images as identity references when present. Individual appearance below describes physical design only.\n'
              'Character: '+person['name']+'\nAppearance: '+person['appearance']+
              '\nMANDATORY SHARED PROJECT STYLE (identical across ALL characters; overrides conflicting inferred rendering styles): '+style)
    nodes = [{'id': node, 'kind': 'image_task', 'text': prompt, 'watermark':False}]
    edges = []
    for i, asset in enumerate(source_assets(spec)):
        if asset['name'] in person['reference_names']:
            ident = 'ref_' + str(i)
            nodes.append({'id': ident, 'kind': 'image', 'asset': asset})
            edges.append({'source': ident, 'target': node})
    return {'nodes': nodes, 'edges': edges}


def prepare(state, store, visitor):
    plan = assess(state)
    if plan.get('status')=='awaiting_clarification' or not plan['required'] or plan.get('model_choice'):
        return plan
    # One shared recommendation; each paid job still binds its own exact input.
    node = 'character_choice_' + plan['id']
    doc = document(state.creative_spec, plan['characters'][0], node)
    doc['nodes'][0]['text'] = ('Select an image model for consistent character turnaround sheets, exactly three full-body views per important character.\nCreative brief: ' + state.creative_spec.source_prompt[:3500] + '\nShared style: '+str(plan.get('shared_style',{}).get('description','')) + '\nImportant characters: ' + json.dumps([{'name':p['name'], 'appearance':p['appearance'][:600]} for p in plan['characters']], ensure_ascii=False))
    plan['model_choice'] = ImageTasks(store).recommend(visitor, node, doc)
    return plan


def running(state, store, visitor):
    return any(ImageTasks(store).get(visitor, p.get('task_id', '')).get('status') == 'running'
               for p in state.creative_spec.character_reference_plan.get('characters', []))


def start(state, store, visitor, plan_id, model):
    plan = state.creative_spec.character_reference_plan
    if not plan or plan.get('id') != plan_id or state.status not in EDITABLE:
        raise ValueError('人物参考规划已变化，请刷新。')
    if plan.get('status')=='awaiting_clarification':
        raise ValueError('请先在创作 Agent 对话中确认风格或其他关键问题。')
    if not plan['required']:
        return plan
    if plan.get('status') != 'planned':
        return sync(state, store, visitor)
    if plan.get('schema_version')!=2 or plan.get('view_count')!=3:
        raise ValueError('请重新规划人物三视图并确认统一风格。')
    if model not in {c['model_id'] for c in cards() if c['configured']}:
        raise ValueError('所选生图模型不可用。')
    if not plan.get('model_choice'):
        raise ValueError('请先获取生图模型推荐。')
    assets = source_assets(state.creative_spec)
    if any(a.get('role') in {'first_frame', 'last_frame'} for a in assets):
        raise ValueError('首尾帧模式不能混用人物三视图参考。请先改用普通参考图模式。')
    if len([a for a in assets if a.get('media_type') == 'image']) + len(plan['characters']) > 30:
        raise ValueError('人物三视图与现有图片合计不能超过 30 张。')
    service = ImageTasks(store)
    jobs = []
    for person in plan['characters']:
        node = 'character_' + plan['id'] + '_' + person['id']
        doc = document(state.creative_spec, person, node)
        choice = {**plan['model_choice'], 'choice_id': uuid.uuid4().hex, 'node_id': node,
                  'fingerprint': signature(inputs(doc, node)), 'status': 'recommended'}
        jobs.append((person, node, doc, choice))
    plan.update(status='generating', selected_model=model)
    # Persist project ownership before launching any paid operation.
    for person, node, doc, choice in jobs:
        person['task_id'] = node
        service.put(visitor, node, choice)
    store.save(visitor, state)
    for person, node, doc, choice in jobs:
        service.start(visitor, node, doc, choice['choice_id'], model)
    return sync(state, store, visitor)


def sync(state, store, visitor):
    plan = state.creative_spec.character_reference_plan
    if not plan or plan.get('status') not in {'generating', 'generated', 'ready', 'failed', 'uncertain'}:
        return plan
    service = ImageTasks(store)
    statuses = []
    for person in plan['characters']:
        job = service.get(visitor, person.get('task_id', ''))
        person.update(status=job.get('status', 'uncertain'), output_url=job.get('output_url', ''))
        statuses.append(person['status'])
    plan['status'] = 'generating' if 'running' in statuses else 'uncertain' if any(s in {'uncertain', 'recommended'} for s in statuses) else 'failed' if 'failed' in statuses else 'ready'
    if plan['status'] == 'ready' and not plan.get('attached'):
        plan['status'] = 'generated'
    return plan


def attach(state, store, visitor, payload, normalise):
    plan = sync(state, store, visitor)
    if not plan or payload.get('plan_id') != plan.get('id'):
        raise ValueError('人物参考规划已变化，请刷新。')
    if plan.get('attached'):
        return plan
    if state.status not in EDITABLE or plan.get('status') != 'generated':
        raise ValueError('请等待人物三视图生成完成。')
    previews = payload.get('previews')
    people = plan['characters']
    if not isinstance(previews, list) or len(previews) != len(people):
        raise ValueError('人物参考预览不完整。')
    assets = source_assets(state.creative_spec)
    for person, preview in zip(people, previews):
        job = ImageTasks(store).get(visitor, person['task_id'])
        if not isinstance(preview, dict) or preview.get('character_id') != person['id'] or preview.get('run_id') != job.get('run_id') or not ImageTasks(store).output(visitor, job.get('run_id')):
            raise ValueError('人物参考结果关联无效。')
        name = 'character-' + plan['id'][:8] + '-' + person['id'] + '.jpg'
        raw = {**preview, 'name': name, 'role': 'reference_image', 'mime_type': 'image/jpeg'}
        asset = normalise({'multimodal_assets': [raw]})[0]
        if asset.get('media_type') != 'image' or not asset.get('preview_data_url', '').startswith('data:image/jpeg;base64,'):
            raise ValueError('人物参考预览格式无效。')
        asset.update(input_order=len(assets) + 1, character_id=person['id'], character_name=person['name'], generated_character_plan=plan['id'])
        assets.append(asset)
    spec = state.creative_spec
    spec.multimodal_assets = assets
    spec.reference_asset = next((a for a in assets if a.get('media_type') == 'image'), None)
    spec.asset_manifest = build_asset_manifest(assets)
    spec.generation_contract = {}; spec.evaluation_contract = {}
    spec.video_model_selection['confirmed'] = False
    plan.update(attached=True, status='ready')
    return plan


def ensure_ready(state):
    plan = state.creative_spec.character_reference_plan
    if plan.get('status')=='awaiting_clarification':
        raise ValueError('请先在创作 Agent 对话中确认关键创意问题。')
    if not plan:
        raise ValueError('请先让 Agent 判断是否需要人物三视图。')
    if plan.get('required') and (plan.get('status') != 'ready' or not plan.get('attached')):
        raise ValueError('请先完成人物三视图，再生成视频。')


def ask(state, questions):
    from datetime import datetime, timezone
    if state.conversation and state.conversation[-1].get('kind')=='creative_clarification':
        return
    for question in questions[:1]:
        state.conversation.append({'id':uuid.uuid4().hex,'at':datetime.now(timezone.utc).isoformat(),'role':'assistant','kind':'creative_clarification','text':question})


def answer(state, text, store, visitor):
    from datetime import datetime, timezone
    plan=state.creative_spec.character_reference_plan
    if plan.get('status')!='awaiting_clarification':
        raise ValueError('没有待确认的创意问题。')
    state.conversation.append({'id':uuid.uuid4().hex,'at':datetime.now(timezone.utc).isoformat(),'role':'user','text':text})
    state.creative_spec.enriched_context.setdefault('creative_answers',[]).append({'kind':'character','question':(plan.get('questions') or [''])[0],'answer':text})
    state.creative_spec.character_reference_plan={}
    try:
        result=prepare(state,store,visitor)
    except Exception:
        state.creative_spec.character_reference_plan=plan
        raise
    return result
