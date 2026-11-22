"""Shared image selection and durable, idempotent local background jobs."""
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from .tools import seedream
from .tools.ark_responses import request_json, configured_model

SKILL=Path(__file__).parent/'agent/skills/image_model_selection.md'
SOURCE='https://docs.volcengine.com/docs/ark/seedream-5-0-pro?lang=zh'


def cards():
    return [{'model_id':model,'name':name,'positioning':position,'configured':bool(os.getenv(seedream.KEYS[model])),
             'max_images':10,'output':'One 2K PNG; watermark follows task settings','price':None,'measured_latency':None,'source':SOURCE}
            for model,name,position in [(seedream.PRO,'Seedream 5.0 Pro','Official positioning: quality, detail and complex creation'),
                                        (seedream.FLASH,'Seedream 5.0 Flash','Official positioning: faster generation and lower cost; latency/cost sensitive use')]]


def inputs(document,node_id):
    nodes={n['id']:n for n in document['nodes']}
    if node_id not in nodes or nodes[node_id]['kind']!='image_task': raise ValueError('Select an image task')
    ordered=[];seen=set()
    def walk(ident):
        if ident in seen:return
        seen.add(ident)
        if not (nodes[ident]['kind']=='image' and nodes[ident].get('image_run_id')):
            for edge in document['edges']:
                if edge['target']==ident:walk(edge['source'])
        ordered.append(nodes[ident])
    walk(node_id)
    if any(n['kind'] in {'audio','video','task','output'} or n['kind']=='image_task' and n['id']!=node_id for n in ordered):
        raise ValueError('Image tasks accept text and images only. Connect a generated image result instead of another task.')
    images=[];names={}
    for n in ordered:
        if n['kind']!='image':continue
        asset=n['asset'];name=asset['name'];value=asset.get('preview_data_url') or asset.get('data_url')
        if name.casefold() in names:
            if names[name.casefold()]!=value:raise ValueError('Reference filenames must be unique.')
            continue
        names[name.casefold()]=value;images.append({'name':name,'url':value})
    prompt='\n\n'.join(n['text'].strip() for n in ordered if n['kind'] in {'text','image_task'} and n['text'].strip())
    if not prompt or len(prompt)>12000: raise ValueError('Write an image prompt of up to 12000 characters')
    if len(images)>10:raise ValueError('Use at most 10 image references')
    return {'prompt':prompt,'images':images,'size':'2K','watermark':nodes[node_id].get('watermark',True)}


def signature(value):
    return hashlib.sha256(json.dumps({'input':value,'cards':cards(),'skill':SKILL.read_text()},sort_keys=True).encode()).hexdigest()


class ImageTasks:
    def __init__(self,store,scope=""):self.store=store;self.scope=scope

    def key(self,node):return self.scope+":"+node if self.scope else node

    def recover(self):
        with self.store.connect() as db:
            for row in db.execute('SELECT visitor_id,node_id,data FROM image_tasks').fetchall():
                result=json.loads(row[2])
                if result.get('status')=='running':
                    result.update(status='uncertain',error='Service restarted during generation. Check the Ark console before retrying; no automatic retry was submitted.')
                    db.execute('UPDATE image_tasks SET data=? WHERE visitor_id=? AND node_id=?',(json.dumps(result),row[0],row[1]))

    def get(self,visitor,node):
        with self.store.connect() as db:
            row=db.execute('SELECT data FROM image_tasks WHERE visitor_id=? AND node_id=?',(visitor,self.key(node))).fetchone()
        return json.loads(row[0]) if row else {}

    def put(self,visitor,node,data):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO image_tasks VALUES (?,?,?)',(visitor,self.key(node),json.dumps(data,ensure_ascii=False)))

    def recommend(self,visitor,node,document):
        previous=self.get(visitor,node)
        if previous.get('status')=='running':return previous
        value=inputs(document,node);available=cards();eligible=[c for c in available if c['configured']]
        if not eligible: raise ValueError('No image model is configured')
        default=next((c for c in eligible if c['model_id']==seedream.FLASH and re.search(r'快速|速度|低成本|便宜|fast|cheap|low.cost',value['prompt'],re.I)),eligible[0])
        result={'choice_id':uuid.uuid4().hex,'fingerprint':signature(value),'recommended_model':default['model_id'],
                'source':'policy_fallback','reason':'Decision model unavailable; capability-based fallback recommendation.','tradeoffs':[],
                'alternatives':[],'candidates':available,'status':'recommended','node_id':node}
        try:
            if not os.getenv('ARK_API_KEY'):raise ValueError('Decision model unavailable')
            decision=request_json(os.environ['ARK_API_KEY'],configured_model(),SKILL.read_text(),
                {'brief':value['prompt'],'reference_images':[{'name':a['name'],'order':i+1} for i,a in enumerate(value['images'])],
                 'eligible_models':eligible,'output':{'size':'2K','count':1,'watermark':value['watermark']}},1400,timeout=90)
            ids={c['model_id'] for c in eligible}
            if decision.get('recommended_model') not in ids or not isinstance(decision.get('reason'),str) or not decision['reason'].strip(): raise ValueError('Invalid recommendation')
            tradeoffs=decision.get('tradeoffs',[]);alternatives=decision.get('alternatives',[])
            if not isinstance(tradeoffs,list) or any(not isinstance(t,str) for t in tradeoffs):raise ValueError('Invalid tradeoffs')
            if not isinstance(alternatives,list) or any(not isinstance(a,dict) or a.get('model_id') not in ids or not isinstance(a.get('reason'),str) for a in alternatives):raise ValueError('Invalid alternatives')
            claims=json.dumps(decision,ensure_ascii=False)
            if re.search(r'[$¥]\s*\d|\d+(?:\.\d+)?\s*元',claims):raise ValueError('Unverified price')
            result.update(recommended_model=decision['recommended_model'],reason=decision['reason'][:1000],tradeoffs=[t[:500] for t in tradeoffs[:4]],alternatives=alternatives[:2],source='doubao_seed_2_1_pro')
        except (ValueError,OSError,TypeError,KeyError):pass
        self.put(visitor,node,result);return result

    def start(self,visitor,node,document,choice_id,model):
        result=self.get(visitor,node)
        if not result or result.get('choice_id')!=choice_id:raise ValueError('Get a fresh image-model recommendation first')
        # Same confirmed operation is never submitted twice, even after completion.
        if result.get('status')!='recommended':return result
        value=inputs(document,node)
        if result['fingerprint']!=signature(value):raise ValueError('Image inputs changed. Recommend a model again.')
        if not isinstance(model,str) or model not in {c['model_id'] for c in cards() if c['configured']}:raise ValueError('Selected image model is unavailable')
        result.update(status='running',selected_model=model,error='',run_id=choice_id)
        self.put(visitor,node,result)
        self._launch(visitor,node,result.copy(),value)
        return result

    def _launch(self,*args):
        threading.Thread(target=self._work,args=args,daemon=True).start()

    def _work(self,visitor,node,result,value):
        try:
            binding='\nReference binding map: '+json.dumps([{'image':i+1,'mention':'@'+a['name']} for i,a in enumerate(value['images'])],ensure_ascii=False) if value['images'] else ''
            image=seedream.generate(result['selected_model'],value['prompt']+binding,[a['url'] for a in value['images']],**({'watermark':False} if value.get('watermark') is False else {}))
            with self.store.connect() as db:
                db.execute('INSERT INTO image_outputs VALUES (?,?,?,?)',(visitor,result['run_id'],'image/png',image))
            result.update(status='succeeded',output_url='/api/image-output/'+result['run_id'])
        except RuntimeError as exc:result.update(status='uncertain',error=str(exc))
        except Exception:
            # Provider responses may contain private inputs; never echo them into the UI.
            result.update(status='failed',error='Image generation failed. Check the model configuration or Ark console; no automatic retry was submitted.')
        self.put(visitor,node,result)

    def output(self,visitor,ident):
        with self.store.connect() as db:
            row=db.execute('SELECT mime,data FROM image_outputs WHERE visitor_id=? AND id=?',(visitor,ident)).fetchone()
        return (row[0],row[1]) if row else None
