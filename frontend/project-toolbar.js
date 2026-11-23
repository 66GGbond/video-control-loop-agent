/* The toolbar always identifies the active VCL project, independently of canvas. */
(() => {
  const name=document.querySelector('#active-project-name'), form=document.querySelector('#project-name-form');
  const edit=document.querySelector('#edit-project-name'), picker=document.querySelector('#project-picker');
  const status=document.querySelector('#project-toolbar-status');
  let state=null, projects=[], loading=false, saving=false, editing=false;
  function error(message){status.textContent=I18n.text(message);status.hidden=!message;}
  function paint(){
    if(!state)return;
    name.setAttribute('aria-label',I18n.text('Project name'));
    if(!editing)name.value=state.project_name||I18n.text('Untitled project');
    picker.replaceChildren();
    const placeholder=new Option(I18n.text('Choose project'),'');picker.add(placeholder);
    for(const project of projects.filter(p=>p.kind==='vcl')){
      const option=new Option(project.title,project.id);option.translate=false;picker.add(option);
    }
    picker.value='';
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
  picker.addEventListener('focus',load);
  picker.addEventListener('change',async()=>{
    const id=picker.value;if(!id||id===state?.project_id){paint();return;}
    picker.disabled=true;error('');
    try{
      await window.CanvasImages.flush();
      const result=await endpoint('/api/projects/open',{method:'POST',body:JSON.stringify({project_id:id})});
      location.assign('/?project='+encodeURIComponent(result.project_id)+'&opened='+Date.now()+'#vcl');
    }catch(e){error(e.message);paint();picker.disabled=false;}
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
