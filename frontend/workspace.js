/* VCL and saved-project navigation. */
(() => {
  async function workspace(name,remember=true){
    name=name==='history'?'history':'vcl';
    document.body.dataset.workspace=name;
    document.getElementById('vcl-workspace').hidden=name!=='vcl';
    document.getElementById('history-workspace').hidden=name!=='history';
    document.querySelectorAll('.nav-page').forEach(button=>{
      const active=button.dataset.workspace===name;
      button.classList.toggle('active',active);
      if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
    });
    if(remember)history.replaceState(null,'','#'+name);
    if(name==='history'){setCreativeAgentOpen(false);window.ProjectHistory.load();}
    if(name==='vcl'&&window.VCLBridge.state())scheduleProductionDefaults(window.VCLBridge.state());
  }
  document.querySelectorAll('.nav-page').forEach(button=>button.addEventListener('click',()=>workspace(button.dataset.workspace)));
  document.querySelector('.app-mark').addEventListener('click',()=>workspace('vcl'));
  window.addEventListener('hashchange',()=>workspace(location.hash.slice(1),false));
  const language=document.getElementById('workspace-language');
  function paintLanguage(){language.textContent=I18n.language==='zh'?'EN':'ZH';}
  language.addEventListener('click',()=>I18n.setLanguage(I18n.language==='zh'?'en':'zh'));
  window.addEventListener('languagechange',paintLanguage);paintLanguage();
  workspace(location.hash.slice(1),false);
})();
