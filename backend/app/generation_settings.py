"""Atomic edits to the compact generation-settings table."""
from copy import deepcopy
from .agent.generation_spec import validate_generation_spec
from .agent.duration_planning import build_duration_plan
from .agent.sampling import bind_sampling_to_timeline
from .agent.model_router import recommend, confirm


def configure(state, payload):
    if state.status not in {'planning_ready', 'awaiting_first_video', 'awaiting_repair_upload'}:
        raise ValueError('当前阶段不能更改生成参数。')
    original = state.creative_spec
    spec = deepcopy(original)
    state.creative_spec = spec
    try:
        value = validate_generation_spec({**spec.generation_spec, **{k: payload[k] for k in ('duration_seconds', 'aspect_ratio', 'quality_target','resolution') if k in payload}})
        changed = any(value.get(k) != spec.generation_spec.get(k) for k in ('duration_seconds', 'aspect_ratio', 'quality_target','resolution'))
        if changed:
            value['rationale'] = 'User-adjusted generation settings.'
            timing = build_duration_plan(spec.shots, explicit_duration_seconds=value['duration_seconds'], strategy=spec.production_strategy)
            if timing['mode'] != 'single_clip':
                raise ValueError('该时长无法容纳当前脚本，请增加时长或调整脚本。')
            spec.generation_spec = value
            spec.duration_plan = timing
            spec.shots = timing['timeline']
            spec.evidence_sampling = bind_sampling_to_timeline(spec.evidence_sampling, spec.shots)
            spec.generation_contract = {}; spec.evaluation_contract = {}
        # Only reevaluate when inputs changed. The stored recommendation remains the default.
        choice = recommend(state)
        manual = payload.get('model_changed') is True
        model = payload.get('model_id') if manual else choice.get('user_model') or (choice.get('recommended_model') if changed else choice.get('selected_model') or choice.get('recommended_model'))
        if not model:
            raise ValueError('没有可用的视频模型，请检查模型配置。')
        card=next((c for c in choice['candidates'] if c['model_id']==model),None)
        if not card:raise ValueError('所选模型不可用。')
        if spec.generation_spec.get('resolution') not in card['resolutions']:
            if 'resolution' in payload and not manual:raise ValueError('当前模型不支持此清晰度。')
            spec.generation_spec['resolution']=card['resolution']
            spec.generation_contract={};spec.evaluation_contract={}
            choice=recommend(state)
        confirm(state, model, choice['fingerprint'])
        if manual:
            choice['user_model'] = model
        return choice
    except Exception:
        state.creative_spec = original
        raise
