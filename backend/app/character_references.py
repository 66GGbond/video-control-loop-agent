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
    return {'required': value['required'], 'characters': result}


def source_assets(spec):
    return [a for a in (spec.multimodal_assets or ([spec.reference_asset] if spec.reference_asset else [])) if not a.get('generated_character_plan')]


def source_reference(spec):
    return next((a for a in source_assets(spec) if a.get('media_type') == 'image' or a.get('mime_type', '').startswith('image/')), None)


def assess(state):
    spec = state.creative_spec
    if state.status not in EDITABLE or not spec.shots:
        raise ValueError('请先生成脚本，再规划人物参考。')
    if spec.character_reference_plan:
        return spec.character_reference_plan
    if not os.getenv('ARK_API_KEY'):
        raise ValueError('人物参考规划需要配置决策模型。')
    language = 'English' if spec.enriched_context.get('ui_language') == 'en' else 'Simplified Chinese'
    decision = request_json(os.environ['ARK_API_KEY'], configured_model(), SKILL.read_text() + '\nInterface language: ' + language,
                            {'brief': spec.source_prompt, 'storyboard': spec.shots, 'visual_treatment': spec.production_direction,
                             'uploaded_images': [{'name': a['name'], 'role': a.get('role')} for a in source_assets(spec) if a.get('media_type') == 'image']}, 3000, timeout=90)
    plan = validate_plan(decision, source_assets(spec))
    plan.update(id=uuid.uuid4().hex, status='planned' if plan['required'] else 'skipped')
    spec.character_reference_plan = plan
    return plan


def document(spec, person, node):
    prompt = ('Create a professional character turnaround sheet for ONE important video character. '
              'Exactly four equal full-body views, left to right: front, back, left profile, right profile. '
              'Same single identity, proportions, face, hairstyle and outfit across all four views. Neutral uncluttered background, even studio light, readable face and clothing. '
              'No extra characters, no plot scene, no text, no labels. Use supplied character images as identity reference when present.\n'
              'Character: ' + person['name'] + '\nAppearance / adjustable casting default: ' + person['appearance'] +
              '\nVideo visual treatment: ' + str(spec.production_direction.get('visual_treatment', '')))
    nodes = [{'id': node, 'kind': 'image_task', 'text': prompt}]
    edges = []
    for i, asset in enumerate(source_assets(spec)):
        if asset['name'] in person['reference_names']:
            ident = 'ref_' + str(i)
            nodes.append({'id': ident, 'kind': 'image', 'asset': asset})
            edges.append({'source': ident, 'target': node})
    return {'nodes': nodes, 'edges': edges}


def prepare(state, store, visitor):
    plan = assess(state)
    if not plan['required'] or plan.get('model_choice'):
        return plan
    # One shared recommendation; each paid job still binds its own exact input.
    node = 'character_choice_' + plan['id']
    doc = document(state.creative_spec, plan['characters'][0], node)
    doc['nodes'][0]['text'] = ('Select an image model for consistent character turnaround sheets, exactly four full-body views per important character.\nCreative brief: ' + state.creative_spec.source_prompt[:3500] + '\nImportant characters: ' + json.dumps([{'name':p['name'], 'appearance':p['appearance'][:600]} for p in plan['characters']], ensure_ascii=False))
    plan['model_choice'] = ImageTasks(store).recommend(visitor, node, doc)
    return plan


def running(state, store, visitor):
    return any(ImageTasks(store).get(visitor, p.get('task_id', '')).get('status') == 'running'
               for p in state.creative_spec.character_reference_plan.get('characters', []))


def start(state, store, visitor, plan_id, model):
    plan = state.creative_spec.character_reference_plan
    if not plan or plan.get('id') != plan_id or state.status not in EDITABLE:
        raise ValueError('人物参考规划已变化，请刷新。')
    if not plan['required']:
        return plan
    if plan.get('status') != 'planned':
        return sync(state, store, visitor)
    if model not in {c['model_id'] for c in cards() if c['configured']}:
        raise ValueError('所选生图模型不可用。')
    if not plan.get('model_choice'):
        raise ValueError('请先获取生图模型推荐。')
    assets = source_assets(state.creative_spec)
    if any(a.get('role') in {'first_frame', 'last_frame'} for a in assets):
        raise ValueError('首尾帧模式不能混用人物四视图参考。请先改用普通参考图模式。')
    if len([a for a in assets if a.get('media_type') == 'image']) + len(plan['characters']) > 30:
        raise ValueError('人物四视图与现有图片合计不能超过 30 张。')
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
        raise ValueError('请等待人物四视图生成完成。')
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
    if not plan:
        raise ValueError('请先让 Agent 判断是否需要人物四视图。')
    if plan.get('required') and (plan.get('status') != 'ready' or not plan.get('attached')):
        raise ValueError('请先完成人物四视图，再生成视频。')
