"""Runtime decision skill + deterministic eligibility + explicit user confirmation."""
import hashlib
import json
import os
import re
from pathlib import Path
from .model_catalog import candidate_cards, model_cards, SEEDANCE_25, H3, low_cost_requested
from ..tools.ark_responses import request_json, configured_model

SKILL = Path(__file__).with_name('skills') / 'video_model_selection.md'


def fingerprint(state):
    spec=state.creative_spec
    assets=[]
    for asset in spec.multimodal_assets or ([spec.reference_asset] if spec.reference_asset else []):
        assets.append({k:hashlib.sha256(v.encode()).hexdigest() if k in {'data_url','preview_data_url'} and isinstance(v,str) else v for k,v in asset.items()})
    value={'routing_skill':hashlib.sha256(SKILL.read_bytes()).hexdigest(),'brief':spec.source_prompt,'goal':spec.goal,'output':spec.generation_spec,'assets':assets,
           'constraints':spec.immutable_character_attributes,'direction':spec.production_direction,'shots':spec.shots,'reference_manifest':spec.asset_manifest,
           'repair':state.latest_repair_task.prompt if state.latest_repair_task else None}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def recommend(state):
    signature=fingerprint(state)
    cards=candidate_cards(state)
    eligible=[c for c in cards if c['eligible']]
    previous=state.creative_spec.video_model_selection
    if previous.get('fingerprint')==signature and previous.get('candidates')==cards:
        return previous
    result={'fingerprint':signature,'candidates':cards,'confirmed':False,'selected_model':None,'skill':'video_model_selection_v1',
            'recommended_model':None,'source':'blocked','reason':'没有兼容且已配置的候选模型，请调整需求或补全配置。','tradeoffs':[],'alternatives':[]}
    if eligible:
        chosen=next((c for c in eligible if c['model_id']==(H3 if low_cost_requested(state.creative_spec.source_prompt) else SEEDANCE_25)),eligible[0])
        result.update(recommended_model=chosen['model_id'],source='policy_fallback',reason='决策模型暂不可用；按能力约束和已配置策略给出备用推荐，不代表效果实测排名。')
        try:
            if not os.getenv('ARK_API_KEY'): raise ValueError('No decision model key')
            spec=state.creative_spec
            payload={'brief':spec.source_prompt,'objective':spec.goal,'output_spec':spec.generation_spec,
                     'immutable_constraints':spec.immutable_character_attributes,'director_plan':spec.production_direction,
                     'shot_plan':spec.shots,'repair_priority':state.latest_repair_task.prompt if state.latest_repair_task else None,
                     'reference_manifest':spec.asset_manifest,'eligible_models':eligible,
                     'excluded_models':[{'model_id':c['model_id'],'reasons':c['exclusion_reasons']} for c in cards if not c['eligible']]}
            language='English' if spec.enriched_context.get('ui_language')=='en' else 'Simplified Chinese'
            system=SKILL.read_text()+f'\nWrite user-facing reasons, tradeoffs and alternatives in {language}. Preserve model names, technical terms and quoted user phrases; keep JSON keys and model IDs unchanged.'
            decision=request_json(os.environ['ARK_API_KEY'],configured_model(),system,payload,1800,timeout=90)
            ids={c['model_id'] for c in eligible}
            if decision.get('recommended_model') not in ids or not isinstance(decision.get('reason'),str) or not decision['reason'].strip(): raise ValueError('Invalid model recommendation')
            tradeoffs=decision.get('tradeoffs',[])
            alternatives=decision.get('alternatives',[])
            if not isinstance(tradeoffs,list) or not all(isinstance(v,str) for v in tradeoffs): raise ValueError('Invalid tradeoffs')
            if not isinstance(alternatives,list) or any(not isinstance(a,dict) or a.get('model_id') not in ids or not isinstance(a.get('reason'),str) for a in alternatives): raise ValueError('Invalid alternatives')
            claims=' '.join([decision['reason'],*tradeoffs,*[a['reason'] for a in alternatives]])
            if re.search(r'(?:\$|¥)\s*\d|\d+(?:\.\d+)?\s*元|cheaper than|objectively cheapest|音频(?:分析|理解).{0,6}(?:很差|最差)|poor audio understanding',claims,re.I):
                raise ValueError('Unsupported price or audio-quality claim')
            result.update(recommended_model=decision['recommended_model'],reason=decision['reason'],tradeoffs=tradeoffs[:5],alternatives=alternatives[:3],source='doubao_seed_2_1_pro')
        except (ValueError,OSError,KeyError,TypeError): pass
    state.creative_spec.video_model_selection=result
    state.history.append({'at':'local-demo','phase':'reason','event':'video_model_recommended','payload':{'recommended_model':result['recommended_model'],'reason':result['reason'],'source':result['source'],'excluded':[{'model_id':c['model_id'],'reasons':c['exclusion_reasons']} for c in cards if not c['eligible']]}})
    return result


def confirm(state,model_id,signature):
    choice=state.creative_spec.video_model_selection
    if signature!=fingerprint(state) or choice.get('fingerprint')!=signature: raise ValueError('需求已变化，请重新获取模型推荐。')
    cards=candidate_cards(state)
    if choice.get('candidates')!=cards: raise ValueError('模型配置已变化，请重新获取推荐并确认。')
    card=next((c for c in cards if c['model_id']==model_id and c['eligible']),None)
    if not card: raise ValueError('该模型不符合当前需求或使用策略。')
    choice.update(selected_model=model_id,confirmed=True,accepted_warnings=card['warnings'])
    return card


def confirmed_model(state):
    choice=state.creative_spec.video_model_selection
    if not choice.get('confirmed') or choice.get('fingerprint')!=fingerprint(state): raise ValueError('请先查看推荐并确认本次视频生成模型。')
    current_cards=candidate_cards(state)
    if current_cards!=choice.get('candidates'): raise ValueError('模型配置已变化，请重新获取推荐并确认。')
    card=next((c for c in current_cards if c['model_id']==choice.get('selected_model') and c['eligible']),None)
    if not card: raise ValueError('已选模型当前不可用或不再符合需求，请重新选择。')
    return card['model_id']


class RoutingVideoGenerator:
    def __init__(self):
        from ..tools.seedance import SeedanceVideoGenerator
        from ..tools.minimax_h3 import MiniMaxH3Generator
        self.adapters={c['model_id']:SeedanceVideoGenerator(c['model_id']) for c in model_cards() if c['model_id']!=H3}
        self.adapters[H3]=MiniMaxH3Generator()

    def available(self): return any(a.available() for a in self.adapters.values())
    def select_tool(self,state): return self.adapters[confirmed_model(state)].select_tool(state)
    def submit(self,state,version_id,repair_task=None):
        model=confirmed_model(state)
        if state.creative_spec.generation_contract.get('provider_binding',{}).get('model_id') != model:
            raise ValueError('Generation contract is not bound to the confirmed model')
        return self.adapters[model].submit(state,version_id,repair_task)
    def refresh(self,job):
        model=job.model_id
        if model not in self.adapters: raise ValueError('Generation job belongs to an unknown model; no provider was switched')
        return self.adapters[model].refresh(job)
