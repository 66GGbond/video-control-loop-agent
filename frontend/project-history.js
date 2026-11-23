/* Saved projects are first-class workspaces, with isolated canvas drafts. */
window.ProjectHistory=(()=>{
  const grid=document.querySelector('#history-projects'),status=document.querySelector('#history-status');
  let projects=[],poll=null,loading=false,naming=false,editing=null,typeFilter='all';
  const icon=name=>`<svg class="ui-icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  function message(text){status.textContent=I18n.text(text);}
  function renderCards(){
    if(editing)return;
    grid.replaceChildren();
    for(const project of projects.filter(p=>typeFilter==='all'||p.kind===typeFilter)){
      const card=document.createElement('article');card.className='history-card';
      const open=document.createElement('button');open.type='button';open.className='history-open';
      open.setAttribute('aria-label',I18n.text('Open project')+' · '+project.title);
      const preview=document.createElement('div');preview.className='history-preview';
      if(project.preview_kind){const image=document.createElement('img');image.src=project.preview_url;image.alt=project.title;image.loading='lazy';image.addEventListener('error',()=>{image.remove();preview.innerHTML=icon('canvas');},{once:true});preview.append(image);}
      else preview.innerHTML=icon('canvas');
      if(project.preview_kind==='video'){const badge=document.createElement('span');badge.className='history-video-badge';badge.innerHTML=icon('video');preview.append(badge);}
      const title=document.createElement('h2');title.textContent=project.title;title.translate=false;
      open.append(preview,title);open.addEventListener('click',()=>openProject(project,open));
      const footer=document.createElement('div');footer.className='history-card-footer';
      const date=document.createElement('time');date.dateTime=project.updated_at;date.textContent=new Intl.DateTimeFormat(I18n.language==='zh'?'zh-CN':'en',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(project.updated_at));date.translate=false;
      const rename=document.createElement('button');rename.type='button';rename.className='history-rename';rename.innerHTML=icon('pencil');rename.title=I18n.text('Rename project');rename.setAttribute('aria-label',rename.title+' · '+project.title);
      rename.addEventListener('click',()=>renameProject(project,card,title));
      const type=document.createElement('span');type.className='history-project-type';type.textContent=project.kind==='canvas'?I18n.text('Canvas project'):'VCLA Agent';footer.append(type,date,rename);card.append(open,footer);grid.append(card);
    }
    message(projects.length?'':'No saved projects yet');
  }
  function renameProject(project,card,title){
    if(editing)return;
    editing=project.id;
    const form=document.createElement('form');form.className='history-rename-form';
    const input=document.createElement('input');input.type='text';input.value=project.title;input.maxLength=80;input.required=true;input.setAttribute('aria-label',I18n.text('Project name'));
    const save=document.createElement('button');save.type='submit';save.className='history-rename-save';save.innerHTML=icon('check');save.setAttribute('aria-label',I18n.text('Save project name'));
    form.append(input,save);title.hidden=true;card.append(form);input.focus();input.select();let saving=false;
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(saving)return;if(!input.value.trim()){input.focus();return;}
      saving=true;save.disabled=true;
      try{const data=await endpoint('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:project.id,title:input.value.trim()})});projects=data.projects;editing=null;renderCards();}
      catch(error){message(error.message);saving=false;save.disabled=false;}
    });
    input.addEventListener('keydown',event=>{if(event.key==='Escape'){editing=null;renderCards();}});
  }
  async function openProject(project,button){
    button.disabled=true;clearTimeout(poll);
    try{
      await window.CanvasImages.flush();
      const data=await endpoint('/api/projects/open',{method:'POST',body:JSON.stringify({project_id:project.id})});
      location.assign('/?project='+encodeURIComponent(data.project_id)+'&opened='+Date.now()+'#'+data.workspace);
    }catch(error){message(error.message);button.disabled=false;}
  }
  async function load(){
    if(loading)return;loading=true;clearTimeout(poll);
    try{
      const data=await endpoint('/api/projects');projects=data.projects;renderCards();
      if(projects.some(p=>p.title_source==='pending')&&!naming){
        naming=true;
        try{projects=(await endpoint('/api/project-titles',{method:'POST',body:JSON.stringify({language:I18n.language})})).projects;renderCards();}
        finally{naming=false;}
      }
      if(projects.some(p=>['pending','naming'].includes(p.title_source))&&document.body.dataset.workspace==='history')poll=setTimeout(load,1500);
    }catch(error){message(error.message);}finally{loading=false;}
  }
  document.querySelector('#history-new-project').addEventListener('click',()=>window.openNewProject('vcl',true));
  document.querySelectorAll('[data-project-type]').forEach(button=>button.addEventListener('click',()=>{typeFilter=button.dataset.projectType;document.querySelectorAll('[data-project-type]').forEach(tab=>tab.setAttribute('aria-selected',String(tab===button)));renderCards();}));
  window.addEventListener('languagechange',renderCards);
  return {load};
})();
