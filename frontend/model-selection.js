let modelRecommendationPending = null;
let generationSettingsPending = null;

function renderModelSelection(state) {
  const panel=document.querySelector('#model-selection-panel'),spec=state.creative_spec;
  panel.hidden=!Boolean(spec?.goal && spec.shots?.length);
  if(panel.hidden)return;
  const choice=spec.video_model_selection||{},select=document.querySelector('#video-model-select');
  const eligible=(choice.candidates||[]).filter(c=>c.eligible);
  select.replaceChildren(...eligible.map(card=>{
    const option=document.createElement('option');option.value=card.model_id;
    option.textContent=card.name+(card.model_id===choice.recommended_model?' · '+I18n.text('Recommended'):'');return option;
  }));
  select.value=choice.selected_model||choice.recommended_model||'';
  if(!select.options.length){const option=document.createElement('option');option.textContent=I18n.text(modelRecommendationPending?'Selecting model…':'No compatible model');select.append(option);}
  const output=spec.generation_spec||{};
  document.querySelector('#video-ratio').value=output.aspect_ratio||'16:9';
  document.querySelector('#video-duration').value=output.duration_seconds||4;
  document.querySelector('#video-quality').value=output.quality_target||'standard';
  const active=['planning_ready','awaiting_first_video','awaiting_repair_upload'].includes(state.status);
  panel.querySelectorAll('input,select').forEach(el=>el.disabled=!active||Boolean(generationSettingsPending)||Boolean(modelRecommendationPending));
  select.disabled=select.disabled||!eligible.length;
  const card=(choice.candidates||[]).find(c=>c.model_id===select.value);
  document.querySelector('#video-resolution').textContent=card?.resolution||'—';
  const warnings=card?.warnings||[];
  const notice=document.querySelector('#model-warning');notice.hidden=!warnings.length;
  notice.title=warnings.map(text=>I18n.text(text)).join('\n');notice.setAttribute('aria-label',notice.title||I18n.text('Model constraints'));
  document.querySelector('#settings-status').textContent=I18n.text(generationSettingsPending?'Saving…':'');
}
async function recommendCurrentVideoModel() {
  if(modelRecommendationPending)return modelRecommendationPending;
  modelRecommendationPending=endpoint('/api/recommend-video-model',{method:'POST'}).then(state=>{render(state);return state;}).finally(()=>{
    modelRecommendationPending=null;if(renderedState)renderModelSelection(renderedState);
  });return modelRecommendationPending;
}
async function saveGenerationSettings(modelChanged=false) {
  if(generationSettingsPending)return generationSettingsPending;
  if(modelRecommendationPending)await modelRecommendationPending;
  const choice=renderedState?.creative_spec?.video_model_selection;
  if(!choice?.recommended_model)await recommendCurrentVideoModel();
  const payload={model_changed:modelChanged,model_id:document.querySelector('#video-model-select').value,
    aspect_ratio:document.querySelector('#video-ratio').value,duration_seconds:Number(document.querySelector('#video-duration').value),
    quality_target:document.querySelector('#video-quality').value};
  generationSettingsPending=endpoint('/api/configure-generation',{method:'POST',body:JSON.stringify(payload)}).then(state=>{render(state);return state;}).finally(()=>{
    generationSettingsPending=null;if(renderedState)renderModelSelection(renderedState);
  });renderModelSelection(renderedState);return generationSettingsPending;
}
for(const id of ['video-model-select','video-ratio','video-duration','video-quality'])document.querySelector('#'+id).addEventListener('change',async()=>{
  try{await saveGenerationSettings(id==='video-model-select');}catch(error){renderModelSelection(renderedState);I18n.alert(error.message);}
});
window.addEventListener('languagechange',()=>{if(renderedState)renderModelSelection(renderedState);});

let durationSaveTimer=null;
document.querySelector('#video-duration').addEventListener('input',event=>{
  clearTimeout(durationSaveTimer);
  if(!event.target.value||!event.target.checkValidity())return;
  durationSaveTimer=setTimeout(async()=>{try{await saveGenerationSettings();}catch(error){renderModelSelection(renderedState);I18n.alert(error.message);}},500);
});
