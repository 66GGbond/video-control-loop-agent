/* The toolbar identifies the active project. */
(() => {
  const name=document.querySelector('#active-project-name'), form=document.querySelector('#project-name-form');
  const edit=document.querySelector('#edit-project-name'), picker=document.querySelector('#project-picker');
  const status=document.querySelector('#project-toolbar-status');
  const panel=document.querySelector('#project-switch-panel'), search=document.querySelector('#project-search'), list=document.querySelector('#project-switch-list');
  function closePicker(focus=false){panel.hidden=true;picker.setAttribute('aria-expanded','false');if(focus)picker.focus();}
  function paintProjects(){
    list.replaceChildren();
    const matches=projects.filter(p=>p.kind==='vcl'&&p.title.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()));
    for(const project of matches){
      const row=document.createElement('button');row.type='button';row.className='project-switch-row';
      row.innerHTML='<svg class="ui-icon" aria-hidden="true"><use href="#i-folder"/></svg>';
      const title=document.createElement('span');title.textContent=project.title;title.translate=false;row.append(title);
      if(project.id===state?.project_id){row.classList.add('is-current');row.setAttribute('aria-current','true');const check=document.createElement('span');check.className='project-current-check';check.textContent='✓';row.append(check);}
      row.addEventListener('click',()=>openProject(project.id));list.append(row);
    }
    if(!matches.length){const empty=document.createElement('p');empty.className='project-switch-empty';empty.textContent=I18n.text(projects.length?'No matching projects':'No saved projects yet');list.append(empty);}
  }
  let state=null, projects=[], loading=false, saving=false, editing=false;
  function error(message){status.textContent=I18n.text(message);status.hidden=!message;}
  function paint(){
    if(!state)return;
    name.setAttribute('aria-label',I18n.text('Project name'));
    if(!editing)name.value=state.project_name||I18n.text('Untitled project');
    paintProjects();
  }
  async function load(){
    if(loading)return;loading=true;
    try{
      projects=(await endpoint('/api/projects')).projects;
      if(projects.some(p=>p.title_source==='pending'))projects=(await endpoint('/api/project-titles',{method:'POST',body:JSON.stringify({language:I18n.language})})).projects;
      const active=projects.find(p=>p.id===state?.project_id);if(active)state.project_name=active.title;
      paint();
      if(projects.some(p=>p.title_source==='naming'))setTimeout(load,1500);
    }
    catch(e){error(e.message);}finally{loading=false;}
  }
  function finishEdit(){
    editing=false;name.readOnly=true;
    edit.querySelector('use').setAttribute('href','#i-pencil');
    edit.setAttribute('aria-label',I18n.text('Rename project'));edit.title=I18n.text('Rename project');paint();
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!state||saving)return;
    if(!editing){
      editing=true;name.readOnly=false;name.focus();name.select();
      edit.querySelector('use').setAttribute('href','#i-check');
      edit.setAttribute('aria-label',I18n.text('Save project name'));edit.title=I18n.text('Save project name');return;
    }
    const title=name.value.trim();if(!title){name.focus();return;}
    saving=true;edit.disabled=true;error('');
    try{
      // An untouched workspace has no archive yet; persist its name first.
      if(!projects.some(p=>p.id===state.project_id)){
        await endpoint('/api/reset',{method:'POST',body:JSON.stringify({title})});
        location.assign('/?new='+Date.now()+'#vcl');return;
      }
      projects=(await endpoint('/api/projects/rename',{method:'POST',body:JSON.stringify({project_id:state.project_id,title})})).projects;
      state.project_name=title;finishEdit();
    }catch(e){error(e.message);}finally{saving=false;edit.disabled=false;}
  });
  name.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();finishEdit();}});
  async function openProject(id){
    if(id===state?.project_id){closePicker(true);return;}
    picker.disabled=true;error('');
    try{
      const result=await endpoint('/api/projects/open',{method:'POST',body:JSON.stringify({project_id:id})});
      location.assign('/?project='+encodeURIComponent(result.project_id)+'&opened='+Date.now()+'#vcl');
    }catch(e){error(e.message);picker.disabled=false;}
  }
  picker.addEventListener('click',()=>{
    if(!panel.hidden){closePicker();return;}
    panel.hidden=false;picker.setAttribute('aria-expanded','true');search.value='';paintProjects();search.focus();load();
  });
  search.addEventListener('input',paintProjects);
  document.addEventListener('pointerdown',event=>{if(!event.target.closest('.project-switcher'))closePicker();});
  panel.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();closePicker(true);}
    if(['ArrowDown','ArrowUp'].includes(event.key)){
      event.preventDefault();const rows=[...list.querySelectorAll('button')];if(!rows.length)return;
      const index=rows.indexOf(document.activeElement),step=event.key==='ArrowDown'?1:-1;
      rows[(index+step+rows.length)%rows.length].focus();
    }
  });
  window.addEventListener('vclstatechange',event=>{
    const changed=state?.project_id!==event.detail.project_id;state=event.detail;paint();if(changed)load();
  });
  window.addEventListener('projectnameschange',event=>{
    projects=event.detail;const active=projects.find(p=>p.id===state?.project_id);
    if(active)state.project_name=active.title;paint();
  });
  window.addEventListener('languagechange',paint);
  state=window.VCLBridge?.state();if(state){paint();load();}
})();
