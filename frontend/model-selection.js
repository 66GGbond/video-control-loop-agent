let modelRecommendationPending = null;
let modelDraftSelection = null;

function renderModelSelection(state) {
  const panel=document.querySelector('#model-selection-panel');
  const spec=state.creative_spec;
  const hasPlan=Boolean(spec?.goal && spec.shots?.length);
  panel.hidden=!hasPlan;
  if (!hasPlan) return;
  const choice=spec.video_model_selection || {};
  const select=document.querySelector('#video-model-select');
  select.replaceChildren();
  const eligible=(choice.candidates || []).filter(card=>card.eligible);
  if(modelDraftSelection?.fingerprint!==choice.fingerprint || !eligible.some(c=>c.model_id===modelDraftSelection?.model)) modelDraftSelection=null;
  for (const card of eligible) {
    const option=document.createElement('option');
    option.value=card.model_id;
    option.textContent=card.name+(card.model_id===choice.recommended_model?' · Recommended':'');
    select.append(option);
  }
  select.value=modelDraftSelection?.model || choice.selected_model || choice.recommended_model || '';
  const active=['planning_ready','awaiting_first_video','awaiting_repair_upload'].includes(state.status);
  select.disabled=!active || !eligible.length;
  document.querySelector('#confirm-video-model').disabled=!active || !eligible.length;
  document.querySelector('#recommend-video-model').disabled=!active || Boolean(modelRecommendationPending);
  document.querySelector('#model-recommendation-reason').textContent=choice.reason || '获取模型推荐后，确认本次使用的模型。';
  document.querySelector('#model-selection-status').textContent=choice.confirmed && select.value===choice.selected_model?'Model confirmed':choice.recommended_model?'Awaiting model confirmation':'Model selection';
  const list=document.querySelector('#model-candidate-list');
  list.replaceChildren();
  for (const card of choice.candidates || []) {
    const row=document.createElement('li');
    const title=document.createElement('strong');
    title.textContent=card.name;
    const detail=document.createElement('span');
    const alternative=(choice.alternatives||[]).find(item=>item.model_id===card.model_id);
    const fragments=card.eligible?card.strengths:card.exclusion_reasons;
    fragments.forEach((text,index)=>{if(index)detail.append(document.createTextNode(card.eligible?' · ':' '));const span=document.createElement('span');span.textContent=text;detail.append(span);});
    if(alternative){detail.append(document.createTextNode(' — '));const explanation=document.createElement('span');explanation.textContent=alternative.reason;explanation.translate=false;detail.append(explanation);}
    row.append(title,detail);list.append(row);
  }
  const warnings=document.querySelector('#model-selection-warnings');
  warnings.replaceChildren();
  const selected=(choice.candidates || []).find(c=>c.model_id===select.value);
  for (const warning of [...(selected?.warnings || []),...(choice.tradeoffs || [])]) {
    const item=document.createElement('li');item.textContent=warning;warnings.append(item);
  }
  document.querySelector('#model-selection-source').textContent=choice.source==='doubao_seed_2_1_pro'?'Doubao Seed 2.1 Pro recommendation':choice.source==='policy_fallback'?'Policy fallback recommendation':'Capability screening';
}

async function recommendCurrentVideoModel() {
  if (modelRecommendationPending) return modelRecommendationPending;
  modelRecommendationPending=endpoint('/api/recommend-video-model',{method:'POST'}).then(state=>{render(state);return state;}).finally(()=>{
    modelRecommendationPending=null;
    if (renderedState) renderModelSelection(renderedState);
  });
  return modelRecommendationPending;
}

document.querySelector('#recommend-video-model').addEventListener('click',async()=>{
  try {await recommendCurrentVideoModel();} catch(error){I18n.alert(error.message);}
});
document.querySelector('#video-model-select').addEventListener('change',()=>{
  const choice=renderedState?.creative_spec?.video_model_selection || {};
  modelDraftSelection={fingerprint:choice.fingerprint,model:document.querySelector('#video-model-select').value};
  document.querySelector('#model-selection-status').textContent=choice.confirmed && modelDraftSelection.model===choice.selected_model?'Model confirmed':'Awaiting model confirmation';
  const warnings=document.querySelector('#model-selection-warnings');warnings.replaceChildren();
  const card=(choice.candidates||[]).find(c=>c.model_id===document.querySelector('#video-model-select').value);
  for(const text of card?.warnings||[]) {const item=document.createElement('li');item.textContent=text;warnings.append(item);}
});
document.querySelector('#confirm-video-model').addEventListener('click',async()=>{
  const button=document.querySelector('#confirm-video-model');button.disabled=true;
  try {
    render(await endpoint('/api/confirm-video-model',{method:'POST',body:JSON.stringify({model_id:document.querySelector('#video-model-select').value,fingerprint:renderedState.creative_spec.video_model_selection.fingerprint})}));
  }catch(error){I18n.alert(error.message);button.disabled=false;}
});
