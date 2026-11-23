"""Auditable model cards: documented capability vs implemented adapter limits."""
import os
import re

SEEDANCE_25 = 'doubao-seedance-2-5-260628'
SEEDANCE_20 = 'doubao-seedance-2-0-260128'
H3 = 'MiniMax-H3'
SEEDANCE_SOURCE = 'https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh'
H3_SOURCE = 'https://platform.minimax.cn/docs/api-reference/video-generation-v2-create'
AUDIO_NOTICE = '参考音频不等于逐字台词、音色、节奏或口型必然准确；当前项目尚无 ASR、节拍和口型质量验收工具，关键音频结果需要人工复核。'


def low_cost_requested(text):
    pattern = r'最低成本|低成本|尽量省钱|节省成本|价格优先|少花钱|最低预算|成本优先|预算优先|低预算|预算有限|尽可能省钱|越便宜越好|最便宜|最低价|最低费用|lowest cost|cheapest|minimum cost|low budget|as cheap as possible'
    for match in re.finditer(pattern, text.lower()):
        prefix = text[max(0, match.start()-14):match.start()].lower()
        if not re.search(r'不要|不是|并非|不要求|无需|不必|不考虑|不以|not\s+|no\s+|without\s+', prefix):
            return True
    return False


def silent_requested(text):
    return bool(re.search(r'无声视频|静音视频|不要声音|不要音频|no audio|silent video|muted video',text,re.I))


def model_cards():
    return [
        {'model_id':SEEDANCE_25,'name':'Seedance 2.5','key_env':'SEEDANCE_25_API_KEY','provider_id':'seedance_25',
         'max_seconds':30,'max_images':30,'max_audio':10,'max_video':10,'reference_seconds':30,'audio_only':True,
         'strengths':['4–30 秒输出','可仅用音频参考','原生多模态参考和音画同生'], 'source':SEEDANCE_SOURCE},
        {'model_id':SEEDANCE_20,'name':'Seedance 2.0','key_env':'SEEDANCE_20_API_KEY','provider_id':'seedance_20',
         'max_seconds':15,'max_images':9,'max_audio':3,'max_video':3,'reference_seconds':15,'audio_only':False,
         'strengths':['4–15 秒输出','图片、视频与音频组合参考','原生音画同生'], 'source':SEEDANCE_SOURCE},
        {'model_id':H3,'name':'MiniMax H3','key_env':'MINIMAX_API_KEY','provider_id':'minimax_h3_adapter',
         'max_seconds':15,'max_images':9,'max_audio':3,'max_video':0,'reference_seconds':15,'audio_only':True,
         'strengths':['当前适配器 768P / 2K','图片和音频参考','用户指定的低成本候选；实际价格未核验'],
         'source':H3_SOURCE,'adapter_notice':'当前 H3 适配器尚未接入视频参考；这是适配器边界，不是模型不支持视频。'},
    ]


def candidate_cards(state):
    assets=state.creative_spec.multimodal_assets or ([state.creative_spec.reference_asset] if state.creative_spec.reference_asset else [])
    assets=[a for a in assets if not a.get('local_only')]
    counts={kind:sum(a.get('media_type')==kind for a in assets) for kind in ('image','audio','video')}
    duration=state.creative_spec.generation_spec.get('duration_seconds',4)
    cheap=low_cost_requested(state.creative_spec.source_prompt)
    output=[]
    for card in model_cards():
        card={k:v for k,v in card.items() if k!='key_env'} | {'configured':bool(os.getenv(card['key_env'])),'price_per_second':None,'latency':None,'quality_benchmark':None,'adapter_resolution':os.getenv('MINIMAX_VIDEO_RESOLUTION','768P') if card['model_id']==H3 else '720p'}
        errors=[]
        if not card['configured']: errors.append('未配置该模型的专用密钥。')
        if not 4 <= duration <= card['max_seconds']: errors.append(f"不支持当前 {duration} 秒时长。")
        for kind,limit in (('image','max_images'),('audio','max_audio'),('video','max_video')):
            if counts[kind]>card[limit]: errors.append(f"当前 {kind} 参考数量超出适配器上限 {card[limit]}。")
        if counts['audio'] and not counts['image'] and not counts['video'] and not card['audio_only']:
            errors.append('音频参考必须同时提供图片或视频参考。')
        for kind in ('audio','video'):
            clips=[a for a in assets if a.get('media_type')==kind]
            if any(not isinstance(a.get('duration_seconds'),(int,float)) or not 2<=a['duration_seconds']<=card['reference_seconds'] for a in clips):
                errors.append(f'{kind} 参考时长未知或不符合模型范围。')
            elif sum(a['duration_seconds'] for a in clips)>card['reference_seconds']:
                errors.append(f'{kind} 参考总时长超出上限。')
        frame_assets=[a for a in assets if a.get('role') in {'first_frame','last_frame'}]
        if frame_assets and (len(frame_assets)!=len(assets) or len(frame_assets)>2 or sum(a.get('role')=='first_frame' for a in assets)!=1 or sum(a.get('role')=='last_frame' for a in assets)>1):
            errors.append('首尾帧模式与全模态参考不能混用。')
        if frame_assets and card['model_id']==H3:
            errors.append('当前 H3 适配器只接入全模态参考，尚未接入首尾帧模式。')
        if card['model_id']==H3 and silent_requested(state.creative_spec.source_prompt): errors.append('当前 H3 适配器没有关闭音轨的参数。')
        if card['model_id']==H3 and not cheap: errors.append('仅在用户明确提出最低成本或低预算目标时推荐 H3。')
        warnings=[AUDIO_NOTICE] if counts['audio'] or any(t in state.creative_spec.source_prompt.lower() for t in ('对白','口型','台词','配音','lip sync','dialogue','soundtrack')) else []
        if card['model_id']==H3:
            warnings += ['H3 按你的低预算偏好进入候选，实际费用以平台账单为准；复杂音频要求建议人工复核或考虑其他候选。',card['adapter_notice']]
        card['resolutions'] = [os.getenv('MINIMAX_VIDEO_RESOLUTION','768P').upper()] if card['model_id']==H3 else ['480p','720p','1080p']
        card['resolution'] = os.getenv('MINIMAX_VIDEO_RESOLUTION', '768P').upper() if card['model_id']==H3 else '720p'
        card.update(eligible=not errors,exclusion_reasons=errors,warnings=warnings)
        output.append(card)
    return output
