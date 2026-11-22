let characterPlanningPending=null,characterPollTimer=null,productionDefaultsTimer=null;
const productionAttempts=new Set();
let characterError='',characterAttachPending=null;

function renderCharacterReferences(state) {
  const spec=state.creative_spec,section=document.querySelector('#character-references');
  section.hidden=!Boolean(spec.goal&&spec.shots?.length);
  if(section.hidden){clearTimeout(characterPollTimer);characterPollTimer=null;return;}
  const plan=spec.character_reference_plan||{},active=['planning_ready','awaiting_first_video','awaiting_repair_upload'].includes(state.status);
  if(!plan.id&&!active){section.hidden=true;return;}
  const select=document.querySelector('#character-image-model'),before=select.value;
  select.replaceChildren(...(plan.model_choice?.candidates||[]).filter(c=>c.configured).map(c=>{const o=document.createElement('option');o.value=c.model_id;o.textContent=c.name;return o;}));
  select.value=plan.selected_model||before||plan.model_choice?.recommended_model||'';
  if(!select.value)select.value=plan.model_choice?.recommended_model||'';
  select.hidden=!plan.required;select.disabled=!active||plan.status!=='planned'||!plan.model_choice;
  const generate=document.querySelector('#generate-character-references');
  generate.hidden=!plan.required||plan.status!=='planned';generate.disabled=!active||!select.value||Boolean(characterPlanningPending);
  const retry=document.querySelector('#plan-character-references');retry.textContent=I18n.text(plan.status==='generated'?'Prepare video references':'Retry character planning');retry.hidden=(plan.status==='generated'? !characterError : Boolean(plan.status==='skipped'||plan.model_choice||characterPlanningPending))||!active;
  const statuses={planned:'Important characters selected',generating:'Generating character references…',generated:'Preparing video references…',ready:'Character references attached to video',skipped:'No character references needed',failed:'Character generation failed. Check the Ark console before starting a new plan.',uncertain:'Character generation is unconfirmed. Check the Ark console; it will not retry automatically.'};
  document.querySelector('#character-reference-status').textContent=characterError||I18n.text(statuses[plan.status]||'Planning character references…');
  const grid=document.querySelector('#character-reference-grid');grid.replaceChildren();
  for(const person of plan.characters||[]) {
    const card=document.createElement('article');card.className='character-reference-card';
    if(person.output_url){const link=document.createElement('a');link.href=person.output_url;link.target='_blank';link.rel='noopener';link.title=I18n.text('Open original image');const img=document.createElement('img');img.src=person.output_url;img.alt=person.name+' · '+I18n.text('Character four views');link.append(img);card.append(link);}
    else {const placeholder=document.createElement('div');placeholder.className='character-reference-placeholder';placeholder.innerHTML='<svg class="ui-icon" aria-hidden="true"><use href="#i-image"/></svg>';card.append(placeholder);}
    const name=document.createElement('strong');name.textContent=person.name;name.translate=false;card.append(name);
    if(person.status&&person.status!=='succeeded'){const status=document.createElement('small');status.textContent=I18n.text({running:'Generating…',failed:'Failed',uncertain:'Unconfirmed'}[person.status]||'Waiting');card.append(status);}
    grid.append(card);
  }
  if(plan.status==='generated'&&!characterAttachPending&&!characterError)setTimeout(()=>attachCharacterReferences().catch(()=>{}),0);
  if(plan.status==='generating'&&!characterPollTimer)characterPollTimer=setTimeout(pollCharacterReferences,2500);
}
async function prepareCharacterReferences() {
  const plan=renderedState?.creative_spec?.character_reference_plan;
  if(plan?.status==='skipped'||plan?.model_choice||['generated','ready','generating','failed','uncertain'].includes(plan?.status))return renderedState;
  if(characterPlanningPending)return characterPlanningPending;
  characterError='';
  characterPlanningPending=endpoint('/api/plan-character-references',{method:'POST',body:'{}'}).then(state=>{render(state);return state;}).catch(error=>{characterError=I18n.text(error.message);throw error;}).finally(()=>{characterPlanningPending=null;renderCharacterReferences(renderedState);});
  renderCharacterReferences(renderedState);return characterPlanningPending;
}
function scheduleProductionDefaults(state) {
  if(!['planning_ready','awaiting_first_video','awaiting_repair_upload'].includes(state.status)||!state.creative_spec.shots?.length)return;
  const key=state.project_id+JSON.stringify([state.creative_spec.source_prompt,state.creative_spec.shots]);
  if(productionAttempts.has(key))return;
  productionAttempts.add(key);clearTimeout(productionDefaultsTimer);
  productionDefaultsTimer=setTimeout(async()=>{
    try{await prepareCharacterReferences();}catch(_){/* Inline retry; never automatically repeat a failed planning request. */}
    try{await recommendCurrentVideoModel();}catch(error){document.querySelector('#settings-status').textContent=I18n.text(error.message);}
  },0);
}
async function attachCharacterReferences() {
  if(characterAttachPending)return characterAttachPending;
  const plan=renderedState.creative_spec.character_reference_plan;
  characterAttachPending=(async()=>{
    const previews=[];
    for(const person of plan.characters) {
      const response=await fetch(person.output_url);
      if(!response.ok)throw new Error(I18n.text('Unable to prepare the reference image'));
      const blob=await response.blob();
      const preview=await readReferenceFile(new File([blob],person.id+'.png',{type:blob.type}));
      previews.push({...preview,character_id:person.id,run_id:person.output_url.split('/').at(-1)});
    }
    render(await endpoint('/api/attach-character-references',{method:'POST',body:JSON.stringify({plan_id:plan.id,previews})}));
    await recommendCurrentVideoModel();
  })().catch(error=>{characterError=I18n.text(error.message);throw error;}).finally(()=>{characterAttachPending=null;renderCharacterReferences(renderedState);});
  return characterAttachPending;
}
async function pollCharacterReferences() {
  characterPollTimer=null;
  try{render(await endpoint('/api/character-references'));if(renderedState.creative_spec.character_reference_plan?.status==='ready')await recommendCurrentVideoModel();}
  catch(error){characterError=I18n.text(error.message);renderCharacterReferences(renderedState);}
}
document.querySelector('#plan-character-references').addEventListener('click',async()=>{try{characterError='';if(renderedState.creative_spec.character_reference_plan?.status==='generated')await attachCharacterReferences();else await prepareCharacterReferences();}catch(_){}});
document.querySelector('#generate-character-references').addEventListener('click',async event=>{
  const button=event.currentTarget;button.disabled=true;characterError='';
  try{render(await endpoint('/api/generate-character-references',{method:'POST',body:JSON.stringify({plan_id:renderedState.creative_spec.character_reference_plan.id,model_id:document.querySelector('#character-image-model').value})}));}
  catch(error){characterError=I18n.text(error.message);renderCharacterReferences(renderedState);}
});
window.addEventListener('languagechange',()=>{if(renderedState)renderCharacterReferences(renderedState);});
