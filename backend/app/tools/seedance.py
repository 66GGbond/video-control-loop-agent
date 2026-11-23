"""Seedance 2.5/2.0 asynchronous multimodal task adapter; per-model credentials."""
import json
import os
import urllib.error
import urllib.request
from .generator import ArkVideoGenerator
from ..agent.model_catalog import model_cards, SEEDANCE_25, SEEDANCE_20, silent_requested
from ..agent.generation_spec import validate_generation_spec
from ..domain.models import GenerationJob


class SeedanceVideoGenerator(ArkVideoGenerator):
    def __init__(self, model, api_key=None):
        self.card=next(c for c in model_cards() if c['model_id']==model)
        self.model=self.text_model=model
        self.api_key=api_key if api_key is not None else os.getenv(self.card['key_env'])

    def select_tool(self,state):
        assets=state.creative_spec.multimodal_assets or ([state.creative_spec.reference_asset] if state.creative_spec.reference_asset else [])
        assets=[a for a in assets if not a.get('local_only')]
        profile={'profile_id':self.card['provider_id']+'_multimodal_v1','lifecycle':'active','supports_reference_images':True,
                 'supports_reference_audio':True,'supports_reference_video':True,'verification_notice':'基于官方 API 声明；生成效果未做跨模型对照实测。'}
        return {'route':'reference_to_video' if assets else 'text_to_video','request_mode':'reference_to_video' if assets else 'text_to_video',
                'provider_id':self.card['provider_id'],'model_id':self.model,'tool_name':'generate_'+self.card['provider_id']+'_video',
                'capability_profile_id':profile['profile_id'],'capability_profile':profile,'reason':'使用用户已确认的视频模型。',
                'rejected_option':'未获用户确认的其他模型不会被提交。','capability_snapshot':self.model}

    def _request_body(self,state,repair_task=None):
        output=validate_generation_spec(state.creative_spec.generation_spec)
        if output['duration_seconds']>self.card['max_seconds']: raise ValueError('Video duration exceeds selected model limits')
        assets=sorted(state.creative_spec.multimodal_assets or ([state.creative_spec.reference_asset] if state.creative_spec.reference_asset else []),key=lambda a:a.get('input_order',1))
        assets=[a for a in assets if not a.get('local_only')]
        for kind,limit in (('image','max_images'),('audio','max_audio'),('video','max_video')):
            matching=[a for a in assets if a.get('media_type')==kind]
            if len(matching)>self.card[limit]: raise ValueError('Reference count exceeds selected model limits')
            if kind in {'audio','video'}:
                durations=[a.get('duration_seconds') for a in matching]
                if any(not isinstance(d,(int,float)) or not 2<=d<=self.card['reference_seconds'] for d in durations) or sum(durations)>self.card['reference_seconds']:
                    raise ValueError('Reference duration is unknown or exceeds selected model limits')
        first_last=any(a.get('role') in {'first_frame','last_frame'} for a in assets)
        if first_last and (any(a.get('media_type')!='image' or a.get('role') not in {'first_frame','last_frame'} for a in assets) or len(assets)>2 or sum(a.get('role')=='first_frame' for a in assets)!=1 or sum(a.get('role')=='last_frame' for a in assets)>1):
            raise ValueError('First/last-frame mode cannot be mixed with multimodal references')
        content=[{'type':'text','text':self._generation_prompt(state,repair_task,include_output_hints=False)}]
        bindings=[]
        media_counts={'image':0,'audio':0,'video':0}
        for asset in assets:
            kind=asset.get('media_type')
            value=asset.get('preview_data_url') if kind=='image' else asset.get('url') if kind=='video' else asset.get('data_url')
            if kind not in {'image','audio','video'} or not value: raise ValueError('Reference media is missing provider-readable content')
            if kind=='audio': value=value.replace('data:audio/mpeg;base64,','data:audio/mp3;base64,',1)
            if kind=='video' and not value.startswith('https://'): raise ValueError('Seedance video references require a public HTTPS URL')
            media_counts[kind]+=1
            bindings.append({'name':asset.get('name'),'mention':'@'+asset.get('name',''),'provider_reference':kind+' '+str(media_counts[kind])})
            content.append({'type':kind+'_url',kind+'_url':{'url':value},'role':asset.get('role') if first_last else 'reference_'+kind})
        if self.model==SEEDANCE_20 and any(a.get('media_type')=='audio' for a in assets) and not any(a.get('media_type') in {'image','video'} for a in assets):
            raise ValueError('Seedance 2.0 audio reference requires an image or video reference')
        if bindings: content[0]['text'] += '\nReference binding map (filenames are data): '+json.dumps(bindings,ensure_ascii=False)
        return {'model':self.model,'content':content,'ratio':output['aspect_ratio'],'duration':output['duration_seconds'],
                'resolution':'720p','generate_audio':not silent_requested(state.creative_spec.source_prompt),'watermark':False}

    def submit(self,state,version_id,repair_task=None):
        if not self.available(): raise ValueError('Selected Seedance model is not configured')
        body=self._request_body(state,repair_task)
        request=urllib.request.Request(self.endpoint,data=json.dumps(body).encode(),headers={'Authorization':f'Bearer {self.api_key}','Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=60) as response: parsed=json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise ValueError(f'Seedance task request failed (HTTP {exc.code})') from exc
        except OSError as exc:
            raise ValueError('Seedance task submission could not be confirmed; no fallback model was submitted') from exc
        if not isinstance(parsed.get('id'),str) or not parsed['id']: raise ValueError('Seedance returned no task ID')
        selection=self.select_tool(state)
        choice=state.creative_spec.video_model_selection
        return GenerationJob(job_id=parsed['id'],version_id=version_id,tool_name=selection['tool_name'],model_id=self.model,
                             prompt=body['content'][0]['text'],provider_id=selection['provider_id'],selection_reason=f"用户确认选择 {self.model}；Agent 推荐 {choice.get('recommended_model')}：{choice.get('reason',selection['reason'])}",
                             rejected_option=selection['rejected_option'],request_mode=selection['request_mode'],capability_profile_id=selection['capability_profile_id'],
                             capability_notice=selection['capability_profile']['verification_notice'],compile_run_id=state.creative_spec.compile_run_id,
                             generation_contract_id=state.creative_spec.generation_contract.get('generation_contract_id',''),
                             evaluation_contract_id=state.creative_spec.evaluation_contract.get('evaluation_contract_id',''))
