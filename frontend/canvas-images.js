/* Image generation belongs to the canvas. Confirmation is an explicit user action. */
window.CanvasImages=(()=>{
  const states=new Map(),selected=new Map(),pending=new Set(),polls=new Map();let host;
  const api=async(path,body)=>{
    const response=await fetch(path,{headers:{'Content-Type':'application/json','X-Project-ID':window.VCLBridge.state()?.project_id||'','X-Canvas-ID':host?.getProjectId?.()||''},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Image request failed');return data;
  };
  function controlsDisabled(id){return pending.has(id)||states.get(id)?.status==='running';}
  async function update(id,result){
    states.set(id,result);if(!host.getDocument()?.nodes.some(n=>n.id===id))return;
    host.render();
    if(result.status==='running')poll(id);
    if(result.status==='succeeded')try{await host.addResult(result,id);}catch(error){host.notify(error.message);}
  }
  function poll(id){
    clearTimeout(polls.get(id));polls.set(id,setTimeout(async()=>{
      try{await update(id,await api('/api/image-task/'+id));}catch(error){host.notify(error.message);poll(id);}
    },1800));
  }
  async function action(node,generate){
    if(controlsDisabled(node.id))return;pending.add(node.id);host.render();
    try{
      await host.flush();let result;
      if(generate){const choice=states.get(node.id);if(!choice||choice.status!=='recommended')throw new Error('Recommend an image model first.');
        result=await api('/api/generate-image',{node_id:node.id,choice_id:choice.choice_id,model_id:selected.get(node.id)||choice.recommended_model});}
      else {result=await api('/api/recommend-image-model',{node_id:node.id});selected.set(node.id,result.recommended_model);}
      await update(node.id,result);
    }catch(error){host.notify(error.message);}
    finally{pending.delete(node.id);host.render();}
  }
  function decorate(node,body){
    const result=states.get(node.id),box=document.createElement('div');box.className='image-task-controls';
    const meta=document.createElement('small');meta.textContent='2K · 1 image · Watermark';box.append(meta);
    const busy=controlsDisabled(node.id);
    if(result){
      const label=document.createElement('small');label.className='image-decision-source';label.textContent=result.source==='doubao_seed_2_1_pro'?'Doubao recommendation':'Policy fallback';box.append(label);
      const reason=document.createElement('p');reason.textContent=result.reason;reason.translate=result.source==='policy_fallback';box.append(reason);
      const select=document.createElement('select');select.setAttribute('aria-label','Image model');
      for(const card of result.candidates||[]){const option=document.createElement('option');option.value=card.model_id;option.disabled=!card.configured;option.textContent=card.name+(card.model_id===result.recommended_model?' · '+I18n.text('Recommended'):'');select.append(option);}
      select.value=selected.get(node.id)||result.selected_model||result.recommended_model;select.disabled=busy;
      select.addEventListener('change',()=>selected.set(node.id,select.value));box.append(select);
      const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='Model comparison';details.append(summary);
      for(const card of result.candidates||[]){const line=document.createElement('p');line.textContent=card.name+': '+I18n.text(card.positioning);details.append(line);}
      for(const text of result.tradeoffs||[]){const line=document.createElement('p');line.textContent=text;line.translate=false;details.append(line);}
      for(const alt of result.alternatives||[]){const line=document.createElement('p');line.textContent=alt.model_id+': '+alt.reason;line.translate=false;details.append(line);}
      const notice=document.createElement('small');notice.textContent='Vendor positioning; actual prices and comparative quality are unverified.';details.append(notice);box.append(details);
      if(result.error){const error=document.createElement('p');error.className='image-task-error';error.textContent=result.error;box.append(error);}
    }
    const actions=document.createElement('div');actions.className='image-task-actions';
    const recommend=document.createElement('button');recommend.type='button';recommend.className='secondary';recommend.textContent='Recommend model';recommend.disabled=busy;recommend.addEventListener('click',()=>action(node,false));actions.append(recommend);
    const generate=document.createElement('button');generate.type='button';generate.className='primary';generate.textContent=busy?'Working…':result?.status==='succeeded'?'Image ready':'Confirm and generate';generate.disabled=busy||result?.status!=='recommended';generate.addEventListener('click',()=>action(node,true));actions.append(generate);box.append(actions);body.append(box);
  }
  async function restore(){
    for(const node of host.getDocument().nodes.filter(n=>n.kind==='image_task'))try{
      const result=await api('/api/image-task/'+node.id);if(result.choice_id)await update(node.id,result);
    }catch(error){host.notify(error.message);}
  }
  async function addLibraryItems(list){
    try{
      const data=await api('/api/image-results');
      if(data.results.length)list.querySelector('.canvas-library-empty')?.remove();
      for(const result of data.results){const button=document.createElement('button');button.className='canvas-library-item';button.type='button';button.textContent=result.name;button.addEventListener('click',async()=>{try{await host.addResult(result);}catch(error){host.notify(error.message);}});list.append(button);}
    }catch(error){host.notify(error.message);}
  }
  window.addEventListener('languagechange',()=>{if(host?.getDocument())host.render();});
  return {mount:value=>host=value,decorate,restore,addLibraryItems,flush:()=>host?.flush(),reset(){for(const timer of polls.values())clearTimeout(timer);states.clear();selected.clear();pending.clear();polls.clear();}};
})();
