/* Local, persisted node canvas. All AI work is handed to the existing composer. */
(() => {
  const C=window.CanvasCore, $=id=>document.getElementById(id);
  const root=$('canvas-workspace'),stage=$('canvas-stage'),world=$('canvas-world'),nodes=$('canvas-nodes'),edges=$('canvas-edges');
  const icon=name=>`<svg class="ui-icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const labels={text:'Text',note:'Note',image:'Image',audio:'Audio',video:'Video reference',task:'Video task',image_task:'Image generation',output:'Video version'};
  const kinds={text:'text',note:'note',image:'image',audio:'audio',video:'video',task:'task',image_task:'image-task',output:'video'};
  let doc=null,revision=0,loaded=false,loading=null,dirty=false,saving=false,saveTimer=null,changeId=0,saveError=false,saveBlocked=false,saveFailures=0;
  let selection=new Set(),selectedEdge=null,connecting=null,tool='select',showEdges=true,space=false,gesture=null,addPoint=null;
  let undo=[],redo=[],toastTimer=null,textEditSnapshot=null,currentState=null;
  const id=()=>`n_${crypto.randomUUID().replaceAll('-','')}`;
  function notify(text) {const el=$('workspace-toast');el.textContent=text;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,4300);}
  function status(value,message) {const el=$('canvas-save-status');el.dataset.state=value;el.title=message||({saved:'Draft saved automatically',saving:'Saving draft…',error:'Save failed. Export your draft before reloading.'}[value]);el.setAttribute('aria-label',el.title);}
  function checkpoint(snapshot=C.clone(doc)) {undo.push(snapshot);if(undo.length>24)undo.shift();redo=[];updateControls();}
  function changed() {dirty=true;changeId++;if(saveBlocked)return;saveError=false;status('saving');clearTimeout(saveTimer);saveTimer=setTimeout(save,500);}
  async function save() {
    if(!doc||!dirty||saving||saveError)return;
    saving=true;const at=changeId;
    try {
      const response=await fetch('/api/canvas',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision,document:doc})});
      const data=await response.json();if(!response.ok){const error=new Error(data.error||'Save failed.');error.status=response.status;throw error;}
      revision=data.revision;saveFailures=0;
      if(at===changeId){dirty=false;status('saved');}
    } catch(error) {
      saveError=true;saveFailures++;saveBlocked=error.status>=400&&error.status<500;
      const message=saveBlocked?error.message:'Draft save interrupted. Retrying automatically.';
      status('error',message);if(saveBlocked||saveFailures===1)notify(message);
      if(!saveBlocked){clearTimeout(saveTimer);saveTimer=setTimeout(()=>{saveError=false;save();},Math.min(15000,1000*2**Math.min(saveFailures,4)));}
    }
    finally {saving=false;if(dirty&&!saveError){clearTimeout(saveTimer);saveTimer=setTimeout(save,100);}}
  }
  async function load() {
    if(loaded)return;
    if(loading)return loading;
    loading=(async()=>{
      const response=await fetch('/api/canvas');if(!response.ok)throw new Error('Canvas could not be loaded.');
      const data=await response.json();doc=data.document;revision=data.revision;loaded=true;status('saved');$('canvas-title').value=doc.title==='Untitled canvas'?'':doc.title;
      render();window.CanvasImages.restore();
    })().finally(()=>loading=null);
    return loading;
  }
  async function workspace(name,remember=true) {
    name=name==='canvas'?'canvas':'vcl';document.body.dataset.workspace=name;
    $('vcl-workspace').hidden=name!=='vcl';root.hidden=name!=='canvas';
    const modelPanel=$('model-selection-panel');
    if(name==='canvas')$('creative-agent-feed').insertBefore(modelPanel,$('agent-video-action'));
    else $('vcl-workspace').insertBefore(modelPanel,$('project-status-panel'));
    document.querySelectorAll('[data-workspace].nav-page').forEach(button=>{
      const active=button.dataset.workspace===name;button.classList.toggle('active',active);
      if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
    });
    if(remember){history.replaceState(null,'',name==='canvas'?'#canvas':'#vcl');}
    if(name==='canvas')try {await load();stage.focus({preventScroll:true});}catch(error){notify(error.message);}
  }
  document.querySelectorAll('.nav-page').forEach(button=>button.addEventListener('click',()=>workspace(button.dataset.workspace)));
  document.querySelector('.app-mark').addEventListener('click',()=>workspace('vcl'));
  window.addEventListener('hashchange',()=>workspace(location.hash==='#canvas'?'canvas':'vcl',false));
  function bounds(){return stage.getBoundingClientRect();}
  function local(event){const r=bounds();return {x:event.clientX-r.left,y:event.clientY-r.top};}
  function point(event){const p=local(event);return C.point(p.x,p.y,doc.viewport);}
  function center(){const r=bounds();return C.point(r.width/2,r.height/2,doc.viewport);}
  function freePosition() {
    const middle=center(),base={x:middle.x-C.width/2,y:middle.y-110};
    for(let row=0;row<10;row++)for(const col of [0,1,-1,2,-2]) {
      const candidate={x:base.x+col*(C.width+52),y:base.y+row*300};
      if(!doc.nodes.some(n=>Math.abs(n.x-candidate.x)<C.width+24&&Math.abs(n.y-candidate.y)<270))return candidate;
    }
    return {x:base.x,y:base.y+doc.nodes.length*30};
  }
  function transform() {
    world.style.transform=`translate(${doc.viewport.x}px,${doc.viewport.y}px) scale(${doc.viewport.zoom})`;
    stage.style.backgroundSize=`${24*doc.viewport.zoom}px ${24*doc.viewport.zoom}px`;
    stage.style.backgroundPosition=`${doc.viewport.x}px ${doc.viewport.y}px`;
    $('canvas-zoom').textContent=Math.round(doc.viewport.zoom*100)+'%';
    positionSelection();
  }
  function updateControls() {
    $('canvas-undo').disabled=!undo.length;$('canvas-redo').disabled=!redo.length;
    $('canvas-selection').hidden=!selection.size&&!selectedEdge;
    $('canvas-selection-count').textContent=selectedEdge?'Connection':`${selection.size}`;
    $('canvas-duplicate').disabled=!selection.size;
    const single=selection.size===1?doc?.nodes.find(n=>selection.has(n.id)):null;
    $('canvas-to-agent').hidden=!selection.size||['image_task','output','note'].includes(single?.kind);
    $('canvas-selection-count').hidden=selection.size===1&&!selectedEdge;
    const bar=$('canvas-selection');
    const header=single?[...nodes.children].find(el=>el.dataset.node===single.id)?.querySelector('.canvas-node-header'):null;
    const parent=header||root;
    if(bar.parentElement!==parent)parent.append(bar);
    bar.classList.toggle('is-inline',Boolean(header));
    $('canvas-select').setAttribute('aria-pressed',String(tool==='select'));
    $('canvas-pan').setAttribute('aria-pressed',String(tool==='pan'));
    stage.dataset.tool=space?'pan':tool;
    positionSelection();
  }
  function positionSelection() {
    const bar=$('canvas-selection');if(bar.hidden||root.hidden)return;
    if(bar.classList.contains('is-inline')){bar.style.visibility='visible';return;}
    const region=root.getBoundingClientRect();
    let selected=[...nodes.children].filter(el=>selection.has(el.dataset.node));
    if(!selected.length&&selectedEdge){const ends=selectedEdge.split(':');selected=[...nodes.children].filter(el=>ends.includes(el.dataset.node));}
    if(!selected.length){bar.hidden=true;return;}
    const boxes=selected.map(el=>el.getBoundingClientRect());
    const box={left:Math.min(...boxes.map(r=>r.left)),right:Math.max(...boxes.map(r=>r.right)),top:Math.min(...boxes.map(r=>r.top)),bottom:Math.max(...boxes.map(r=>r.bottom))};
    const visible=box.right>region.left&&box.left<region.right&&box.bottom>region.top&&box.top<region.bottom;
    bar.style.visibility=visible?'visible':'hidden';
    if(!visible)return;
    const width=bar.offsetWidth,height=bar.offsetHeight,gap=10;
    const floor=$('canvas-workspace').querySelector('.canvas-header').getBoundingClientRect().bottom-region.top+10;
    let top=box.top-region.top-height-gap;
    if(top<floor&&box.bottom-region.top+gap+height<region.height-78)top=box.bottom-region.top+gap;
    const x=Math.max(8,Math.min((box.left+box.right)/2-region.left-width/2,region.width-width-8));
    const y=Math.max(floor,Math.min(top,region.height-height-78));
    bar.style.left=x+'px';bar.style.top=y+'px';
  }
  const nodeResize=new ResizeObserver(positionSelection);
  const overlayResize=new ResizeObserver(positionSelection);
  overlayResize.observe(root);overlayResize.observe($('canvas-selection'));
  window.addEventListener('resize',positionSelection);
  function updateSelection() {
    for(const el of nodes.children)el.classList.toggle('selected',selection.has(el.dataset.node));
    updateControls();drawEdges();
  }
  function edgePath(source,target) {
    const start={x:source.x+C.width,y:source.y+24},end={x:target.x,y:target.y+24};
    const bend=Math.max(70,Math.abs(end.x-start.x)*.45);
    return `M ${start.x} ${start.y} C ${start.x+bend} ${start.y}, ${end.x-bend} ${end.y}, ${end.x} ${end.y}`;
  }
  function drawEdges(cursor) {
    edges.replaceChildren();if(!showEdges&&!connecting)return;
    function path(d,className,click) {
      const el=document.createElementNS('http://www.w3.org/2000/svg','path');el.setAttribute('d',d);if(className)el.setAttribute('class',className);
      if(click)el.addEventListener('pointerdown',event=>{event.stopPropagation();selection.clear();selectedEdge=click;updateSelection();});edges.append(el);
    }
    if(showEdges)for(const edge of doc.edges) {
      const source=doc.nodes.find(n=>n.id===edge.source),target=doc.nodes.find(n=>n.id===edge.target);if(!source||!target)continue;
      const key=edge.source+':'+edge.target,d=edgePath(source,target);path(d,selectedEdge===key?'selected':'');path(d,'canvas-edge-hit',key);
    }
    if(connecting&&cursor) {
      const from=doc.nodes.find(n=>n.id===connecting);if(from)path(edgePath(from,{x:cursor.x,y:cursor.y-24}),'edge-draft');
    }
  }
  function button(name,label,action) {
    const el=document.createElement('button');el.type='button';el.className='canvas-icon';el.title=label;el.setAttribute('aria-label',label);el.innerHTML=icon(name);
    el.addEventListener('click',event=>{event.stopPropagation();action();});return el;
  }
  function add(kind,extra={},location=addPoint||freePosition()) {
    if(!loaded)return;
    if(doc.nodes.length>=100){notify('Canvas supports up to 100 nodes.');return;}
    checkpoint();const node={id:id(),kind,x:Math.round(location.x),y:Math.round(location.y),title:I18n.text(labels[kind]),text:'',...extra};
    doc.nodes.push(node);selection=new Set([node.id]);selectedEdge=null;addPoint=null;changed();render();
    requestAnimationFrame(()=>nodes.querySelector(`[data-node="${node.id}"] textarea`)?.focus());return node;
  }
  function render() {
    if(!doc)return;
    // Rebuilding only occurs for structural edits, never per pointer movement or keystroke.
    const actions=$('canvas-selection');if(actions.parentElement!==root)root.append(actions);
    nodeResize.disconnect();nodes.replaceChildren();
    for(const node of doc.nodes) {
      const el=document.createElement('article');el.className=`canvas-node kind-${node.kind}`;el.dataset.node=node.id;el.style.left=node.x+'px';el.style.top=node.y+'px';
      const header=document.createElement('div');header.className='canvas-node-header';header.innerHTML=icon(kinds[node.kind]);
      const title=document.createElement('input');title.value=node.title;title.maxLength=120;title.setAttribute('aria-label','Node title');bindEdit(title,node,'title');header.append(title);
      el.append(header);
      const body=document.createElement('div');body.className='canvas-node-body';
      if(['text','note','task','image_task'].includes(node.kind)) {
        const input=document.createElement('textarea');input.value=node.text;input.maxLength=12000;input.setAttribute('aria-label',labels[node.kind]);input.placeholder=node.kind==='image_task'?'Describe this image…':node.kind==='task'?'Describe this video…':node.kind==='note'?'Note…':'Write a brief…';bindEdit(input,node,'text');body.append(input);
      } else if(node.kind==='image') {
        const img=document.createElement('img');img.src=node.asset.preview_data_url||node.asset.data_url;img.alt=node.asset.name;img.draggable=false;body.append(img);
        img.addEventListener('dblclick',event=>{event.stopPropagation();showImage(node);});
      } else if(node.kind==='audio') {
        const player=document.createElement('audio');player.controls=true;player.preload='none';player.src=node.asset.data_url;body.append(player);
      } else if(node.kind==='video') {
        const box=document.createElement('div');box.className='canvas-media-missing';box.innerHTML=icon('video');body.append(box);
        // Public reference URLs are not fetched automatically just by opening a board.
        const open=document.createElement('button');open.type='button';open.className='canvas-library-item';open.textContent='Preview';open.addEventListener('click',()=>{const player=document.createElement('video');player.controls=true;player.preload='metadata';player.src=node.asset.url;box.replaceWith(player);open.remove();});body.append(open);
      } else if(node.kind==='output') {
        const state=window.VCLBridge.state();const review=state?.project_id===node.project_id?(state.version_reviews||[]).find(v=>v.version_id===node.version_id):null;
        if(review?.asset?.local_video_url) {
          const player=document.createElement('video');player.controls=true;player.preload='none';player.src=review.asset.local_video_url;
          if(review.asset.poster_data_url)player.poster=review.asset.poster_data_url;body.append(player);
        }else {const missing=document.createElement('div');missing.className='canvas-media-missing';missing.textContent='Source project unavailable';body.append(missing);}
      }
      if(node.asset||node.kind==='output') {
        const info=document.createElement('div');info.className='canvas-media-label';const name=document.createElement('span');name.textContent=node.asset?.name||node.version_id.toUpperCase();name.translate=false;
        const seconds=document.createElement('small');seconds.textContent=node.asset?.duration_seconds?node.asset.duration_seconds+'s':node.kind==='output'?node.text:'';seconds.translate=false;info.append(name,seconds);body.append(info);
      }
      if(node.kind==='image_task')window.CanvasImages.decorate(node,body);
      if(node.kind==='image'&&node.image_run_id){const original=document.createElement('a');original.href='/api/image-output/'+node.image_run_id;original.download='seedream-'+node.image_run_id+'.png';original.className='image-original';original.textContent='Original image';body.append(original);}
      el.append(body);
      for(const side of ['in','out']) {
        const port=document.createElement('button');port.type='button';port.className='canvas-port';port.dataset.port=side;port.title=side==='in'?'Connect input':'Connect output';port.setAttribute('aria-label',port.title);
        port.classList.toggle('connecting',connecting===node.id&&side==='out');
        port.addEventListener('click',event=>{event.stopPropagation();if(side==='out'){connecting=connecting===node.id?null:node.id;render();}else connect(node.id);});el.append(port);
      }
      el.addEventListener('pointerdown',event=>{
        if(event.button!==0||space||tool==='pan')return;
        if(event.target.closest('button,audio,video'))return;
        if(event.shiftKey){if(selection.has(node.id))selection.delete(node.id);else selection.add(node.id);}else if(!selection.has(node.id))selection=new Set([node.id]);
        selectedEdge=null;updateSelection();
        if(event.target.closest('input,textarea'))return;
        gesture={type:'nodes',start:local(event),snapshot:C.clone(doc),positions:doc.nodes.filter(n=>selection.has(n.id)).map(n=>({id:n.id,x:n.x,y:n.y})),moved:false};stage.setPointerCapture(event.pointerId);event.preventDefault();
      });nodes.append(el);nodeResize.observe(el);
    }
    $('canvas-empty').hidden=doc.nodes.length>0;transform();updateSelection();updateControls();
  }
  function bindEdit(input,node,key) {
    input.addEventListener('focus',()=>textEditSnapshot=C.clone(doc));
    input.addEventListener('input',()=>{if(textEditSnapshot){checkpoint(textEditSnapshot);textEditSnapshot=null;}node[key]=input.value;changed();});
    input.addEventListener('blur',()=>{textEditSnapshot=null;});
    input.addEventListener('keydown',event=>event.stopPropagation());
  }
  function connect(target) {
    if(!connecting)return;
    try {
      if(!C.canConnect(doc,connecting,target)){notify('This connection is not allowed.');return;}
      if(doc.edges.length>=250){notify('Canvas supports up to 250 connections.');return;}
      checkpoint();doc.edges.push({source:connecting,target});connecting=null;changed();render();
    }catch(error){notify(error.message);}
  }
  function remove(ids=[...selection]) {
    if(!ids.length&&!selectedEdge)return;checkpoint();const set=new Set(ids);
    doc.nodes=doc.nodes.filter(n=>!set.has(n.id));doc.edges=doc.edges.filter(e=>!set.has(e.source)&&!set.has(e.target)&&e.source+':'+e.target!==selectedEdge);
    selection.clear();selectedEdge=null;connecting=null;changed();render();
  }
  function duplicate(ids=[...selection]) {
    if(!ids.length)return;if(doc.nodes.length+ids.length>100){notify('Canvas supports up to 100 nodes.');return;}
    checkpoint();const map=new Map(ids.map(old=>[old,id()]));
    const copies=doc.nodes.filter(n=>map.has(n.id)).map(n=>({...n,id:map.get(n.id),x:n.x+38,y:n.y+38}));
    const connections=doc.edges.filter(e=>map.has(e.source)&&map.has(e.target)).map(e=>({source:map.get(e.source),target:map.get(e.target)}));
    if(doc.edges.length+connections.length>250){undo.pop();notify('Canvas supports up to 250 connections.');return;}
    doc.nodes.push(...copies);doc.edges.push(...connections);selection=new Set(copies.map(n=>n.id));selectedEdge=null;changed();render();
  }
  function historyMove(from,to) {
    if(!from.length)return;to.push(C.clone(doc));doc=from.pop();selection.clear();selectedEdge=null;connecting=null;$('canvas-title').value=doc.title==='Untitled canvas'?'':doc.title;changed();render();
  }
  async function send(target) {
    try {
      let bundle;
      if(target)bundle=C.bundle(doc,target);
      else if(selection.size===1)bundle=C.bundle(doc,[...selection][0]);
      else {
        const synthetic='selection_sink';const temp={...doc,nodes:[...doc.nodes,{id:synthetic,kind:'task',text:''}],edges:[...doc.edges,...[...selection].map(source=>({source,target:synthetic}))]};bundle=C.bundle(temp,synthetic);
      }
      await window.VCLBridge.stage(bundle);notify('Added to Agent draft');
    }catch(error){notify(error.message);}
  }
  function zoom(value,anchor) {if(!doc)return;const r=bounds();doc.viewport=C.zoomAt(doc.viewport,value,anchor?.x??r.width/2,anchor?.y??r.height/2);transform();changed();}
  function fit() {if(!doc)return;const r=bounds();doc.viewport=C.fit(doc.nodes,r.width,r.height);transform();changed();}
  function showMenu(event) {
    if(!doc)return;event.preventDefault();const p=local(event);addPoint=point(event);
    const menu=$('canvas-menu');menu.replaceChildren();
    for(const kind of ['text','image','image_task','video','audio','task','note']) {
      const item=document.createElement('button');item.type='button';item.setAttribute('role','menuitem');item.innerHTML=icon(kinds[kind]);const label=document.createElement('span');label.textContent=labels[kind];item.append(label);
      item.addEventListener('click',()=>{menu.hidden=true;create(kind);});menu.append(item);
    }
    menu.style.left=Math.max(8,Math.min(p.x,root.clientWidth-195))+'px';menu.style.top=Math.max(65,Math.min(p.y,root.clientHeight-305))+'px';menu.hidden=false;
  }
  function create(kind) {
    if(!loaded)return;
    if(kind==='video'){$('canvas-video-dialog').showModal();return;}
    if(kind==='image'||kind==='audio'){const input=$('canvas-file-input');input.accept=kind==='image'?'image/png,image/jpeg,image/webp':'audio/mpeg,audio/mp3,audio/wav,audio/x-wav';input.click();return;}
    add(kind);
  }
  stage.addEventListener('dblclick',event=>{if(!event.target.closest('.canvas-node'))showMenu(event);});
  stage.addEventListener('contextmenu',event=>{if(!event.target.closest('input,textarea,video,audio'))showMenu(event);});
  stage.addEventListener('pointerdown',event=>{
    if(!doc||event.button===2)return;
    $('canvas-menu').hidden=true;
    if(event.target.closest('.canvas-node')&&!space&&tool!=='pan')return;
    if(event.target.closest('.canvas-node input,.canvas-node textarea,.canvas-node button,video,audio'))return;
    stage.focus({preventScroll:true});
    const p=local(event),pan=space||tool==='pan'||event.button===1||event.pointerType==='touch';
    gesture={type:pan?'pan':'marquee',start:p,snapshot:C.clone(doc),moved:false,previous:new Set(event.shiftKey?selection:[])};
    if(!pan){selection=new Set(gesture.previous);selectedEdge=null;updateSelection();}
    else stage.classList.add('is-panning');
    stage.setPointerCapture(event.pointerId);event.preventDefault();
  });
  stage.addEventListener('pointermove',event=>{
    if(!doc)return;
    const p=local(event);
    if(connecting&&!gesture)drawEdges(point(event));
    if(!gesture)return;
    const dx=p.x-gesture.start.x,dy=p.y-gesture.start.y;if(Math.hypot(dx,dy)>3)gesture.moved=true;
    if(gesture.type==='nodes') {
      for(const position of gesture.positions) {
        const node=doc.nodes.find(n=>n.id===position.id);node.x=Math.max(-100000,Math.min(100000,Math.round(position.x+dx/doc.viewport.zoom)));node.y=Math.max(-100000,Math.min(100000,Math.round(position.y+dy/doc.viewport.zoom)));
        const el=nodes.querySelector(`[data-node="${node.id}"]`);el.style.left=node.x+'px';el.style.top=node.y+'px';
      }drawEdges();positionSelection();
    } else if(gesture.type==='pan') {
      doc.viewport.x=gesture.snapshot.viewport.x+dx;doc.viewport.y=gesture.snapshot.viewport.y+dy;transform();
    } else {
      const box=$('canvas-marquee');box.hidden=false;const x=Math.min(p.x,gesture.start.x),y=Math.min(p.y,gesture.start.y),w=Math.abs(dx),h=Math.abs(dy);
      Object.assign(box.style,{left:x+'px',top:y+'px',width:w+'px',height:h+'px'});
      const a=C.point(x,y,doc.viewport),b=C.point(x+w,y+h,doc.viewport);selection=new Set(gesture.previous);
      for(const node of doc.nodes)if(node.x+C.width>=a.x&&node.x<=b.x&&node.y+C.height>=a.y&&node.y<=b.y)selection.add(node.id);updateSelection();
    }
  });
  function endGesture(event) {
    if(!gesture)return;
    if(gesture.moved){if(gesture.type==='nodes')checkpoint(gesture.snapshot);if(gesture.type!=='marquee')changed();}
    else if(gesture.type==='marquee'){selection=gesture.previous;updateSelection();}
    gesture=null;stage.classList.remove('is-panning');$('canvas-marquee').hidden=true;
    if(stage.hasPointerCapture(event.pointerId))stage.releasePointerCapture(event.pointerId);
  }
  stage.addEventListener('pointerup',endGesture);stage.addEventListener('pointercancel',endGesture);
  stage.addEventListener('wheel',event=>{
    if(!doc||event.target.closest('textarea'))return;event.preventDefault();
    if(event.ctrlKey||event.metaKey)zoom(doc.viewport.zoom*Math.exp(-event.deltaY*.008),local(event));
    else {doc.viewport.x-=event.deltaX;doc.viewport.y-=event.deltaY;transform();changed();}
  },{passive:false});
  document.addEventListener('keydown',event=>{
    if(root.hidden||!loaded||event.target.closest('input,textarea,[contenteditable=true]')||document.querySelector('dialog[open]'))return;
    const mod=event.ctrlKey||event.metaKey,key=event.key.toLowerCase();
    if(key==='escape'){connecting=null;selection.clear();selectedEdge=null;$('canvas-menu').hidden=true;$('canvas-library').hidden=true;render();return;}
    if(key===' '){space=true;updateControls();event.preventDefault();return;}
    if(mod&&key==='z'){event.preventDefault();historyMove(event.shiftKey?redo:undo,event.shiftKey?undo:redo);}
    else if(mod&&key==='d'){event.preventDefault();duplicate();}
    else if(mod&&key==='a'){event.preventDefault();selection=new Set(doc.nodes.map(n=>n.id));updateSelection();}
    else if(key==='delete'||key==='backspace'){event.preventDefault();remove();}
    else if(key==='h'||key==='v'){tool=key==='h'?'pan':'select';updateControls();}
    else if(key==='f')fit();
  });
  document.addEventListener('keyup',event=>{if(event.key===' '){space=false;updateControls();}});
  window.addEventListener('blur',()=>{space=false;gesture=null;stage.classList.remove('is-panning');$('canvas-marquee').hidden=true;updateControls();});
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  for(const kind of ['text','image','image_task','video','audio','task','note'])$(kind==='image_task'?'canvas-add-image-task':'canvas-add-'+kind).addEventListener('click',()=>{addPoint=null;create(kind);});
  $('canvas-undo').addEventListener('click',()=>historyMove(undo,redo));$('canvas-redo').addEventListener('click',()=>historyMove(redo,undo));
  $('canvas-select').addEventListener('click',()=>{tool='select';updateControls();});$('canvas-pan').addEventListener('click',()=>{tool='pan';updateControls();});
  $('canvas-zoom-in').addEventListener('click',()=>{if(doc)zoom(doc.viewport.zoom*1.2);});$('canvas-zoom-out').addEventListener('click',()=>{if(doc)zoom(doc.viewport.zoom/1.2);});$('canvas-zoom').addEventListener('click',()=>zoom(1));$('canvas-fit').addEventListener('click',fit);
  $('canvas-show-edges').addEventListener('click',()=>{showEdges=!showEdges;$('canvas-show-edges').setAttribute('aria-pressed',String(showEdges));drawEdges();});
  $('canvas-delete').addEventListener('click',()=>remove());$('canvas-duplicate').addEventListener('click',()=>duplicate());$('canvas-to-agent').addEventListener('click',()=>send());
  $('canvas-title').addEventListener('focus',()=>{if(doc)textEditSnapshot=C.clone(doc);});
  $('canvas-title').addEventListener('input',event=>{if(!doc)return;if(textEditSnapshot){checkpoint(textEditSnapshot);textEditSnapshot=null;}doc.title=event.target.value.trim()||'Untitled canvas';changed();});
  async function importFiles(files) {
    const base=addPoint||freePosition();let offset=0;
    for(const file of files) {
      try {
        const isImage=file.type.startsWith('image/');
        if(!isImage&&!['audio/mpeg','audio/mp3','audio/wav','audio/x-wav'].includes(file.type))throw new Error('Use images, MP3 or WAV. Add videos with a public HTTPS URL.');
        const asset=await window.VCLBridge.readMedia(file);asset.media_type=isImage?'image':'audio';
        if(isImage){asset.preview_data_url=asset.data_url;delete asset.data_url;}
        add(asset.media_type,{asset,title:file.name},{x:base.x+offset,y:base.y+offset});offset+=44;
      }catch(error){notify(error.message);}
    }
  }
  $('canvas-file-input').addEventListener('change',event=>{importFiles([...event.target.files]);event.target.value='';});
  stage.addEventListener('dragover',event=>{if(event.dataTransfer.types.includes('Files'))event.preventDefault();});
  stage.addEventListener('drop',event=>{event.preventDefault();if(!doc)return;addPoint=point(event);importFiles([...event.dataTransfer.files]);});
  $('canvas-video-close').addEventListener('click',()=>$('canvas-video-dialog').close());
  $('canvas-video-form').addEventListener('submit',event=>{
    event.preventDefault();try {
      const url=new URL($('canvas-video-url').value),duration=Number($('canvas-video-duration').value);
      if(url.protocol!=='https:'||url.username||url.password||!Number.isFinite(duration)||duration<2||duration>30)throw new Error('Use a public HTTPS URL and a duration of 2–30 seconds.');
      const name=decodeURIComponent(url.pathname.split('/').at(-1))||'reference.mp4';
      if(name.length>120)throw new Error('Reference filename is too long.');
      add('video',{title:name,asset:{name,media_type:'video',mime_type:name.toLowerCase().endsWith('.mov')?'video/quicktime':'video/mp4',url:url.href,duration_seconds:duration,size:'0',role:'reference_video'}});
      $('canvas-video-dialog').close();$('canvas-video-form').reset();
    }catch(error){notify(error.message);}
  });
  function exportBoard() {
    if(!doc)return;const url=URL.createObjectURL(new Blob([JSON.stringify(doc,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=(doc.title.replace(/[^\p{L}\p{N}_-]+/gu,'_')||'canvas')+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  $('canvas-export').addEventListener('click',exportBoard);$('canvas-import').addEventListener('click',()=>{if(loaded)$('canvas-json-input').click();});
  $('canvas-json-input').addEventListener('change',async event=>{
    const file=event.target.files[0];event.target.value='';if(!file)return;
    try {
      if(file.size>32*1024*1024)throw new Error('Canvas exceeds 32 MB');
      const incoming=JSON.parse(await file.text());
      // The backend is the authority for imported schema/media, and revision prevents stale writes.
      if(saving||dirty)throw new Error('Wait for the canvas to save before importing.');
      if(!window.confirm(I18n.text('Replace this canvas? You can undo this change.')))return;
      const response=await fetch('/api/canvas',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision,document:incoming})});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'Invalid canvas file');
      checkpoint();revision=data.revision;
      const saved=await (await fetch('/api/canvas')).json();doc=saved.document;revision=saved.revision;
      selection.clear();selectedEdge=null;connecting=null;saveError=false;saveBlocked=false;saveFailures=0;clearTimeout(saveTimer);$('canvas-title').value=doc.title==='Untitled canvas'?'':doc.title;status('saved');render();
    }catch(error){notify(error.message);}
  });
  function library() {
    const list=$('canvas-library-items');list.replaceChildren();const state=window.VCLBridge.state();if(!state)return;
    function item(name,kind,action,preview) {
      const el=document.createElement('button');el.type='button';el.className='canvas-library-item';
      if(preview){const img=document.createElement('img');img.src=preview;img.alt='';el.append(img);}else el.innerHTML=icon(kind);
      const label=document.createElement('span');label.textContent=name;label.translate=false;el.append(label);el.insertAdjacentHTML('beforeend',icon('plus'));
      el.addEventListener('click',async()=>{el.disabled=true;try {await action();}catch(error){notify(error.message);}finally{el.disabled=false;}});list.append(el);
    }
    const spec=state.creative_spec;
    if(spec?.source_prompt)item('Brief','text',()=>add('text',{text:spec.source_prompt,title:'Brief'}));
    (spec?.multimodal_assets||[]).forEach((asset,index)=>item(asset.name,asset.media_type,async()=>{
      const response=await fetch('/api/canvas-reference',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:state.project_id,index})});
      const data=await response.json();if(!response.ok)throw new Error(data.error);add(data.asset.media_type,{asset:data.asset,title:data.asset.name});
    },asset.preview_data_url));
    (state.version_reviews||[]).filter(review=>review.asset?.local_video_url).forEach(review=>item(review.version_id.toUpperCase(),'video',()=>add('output',{title:review.version_id.toUpperCase(),version_id:review.version_id,project_id:state.project_id,text:review.status}),review.asset.poster_data_url));
    window.CanvasImages.addLibraryItems(list);
    if(!list.children.length){const empty=document.createElement('p');empty.className='canvas-library-empty';empty.textContent='No project assets yet';list.append(empty);}
  }
  $('canvas-assets').addEventListener('click',()=>{$('canvas-library').hidden=!$('canvas-library').hidden;if(!$('canvas-library').hidden)library();});$('canvas-library-close').addEventListener('click',()=>$('canvas-library').hidden=true);
  function showImage(node) {
    let dialog=$('canvas-image-preview');if(!dialog){dialog=document.createElement('dialog');dialog.id='canvas-image-preview';dialog.className='canvas-dialog canvas-image-preview';dialog.addEventListener('click',()=>dialog.close());document.body.append(dialog);}
    dialog.replaceChildren();const img=document.createElement('img');img.src=node.asset.preview_data_url||node.asset.data_url;img.alt=node.asset.name;img.style.width='100%';dialog.append(img);dialog.showModal();
  }
  window.addEventListener('vclstatechange',event=>{
    const prior=currentState;currentState=event.detail;
    if(!root.hidden&&!$('canvas-library').hidden)library();
    if(doc?.nodes.some(n=>n.kind==='output') && prior?.project_id!==currentState.project_id)render();
  });
  document.addEventListener('pointerdown',event=>{if(!event.target.closest('#canvas-menu')&&!event.target.closest('#canvas-stage'))$('canvas-menu').hidden=true;});
  window.CanvasImages.mount({getDocument:()=>doc,render,notify,changed,add,checkpoint,
    async flush(){while(saving)await new Promise(resolve=>setTimeout(resolve,50));if(saveError)throw new Error('Wait for the draft to finish saving.');clearTimeout(saveTimer);await save();if(dirty)throw new Error('Wait for the draft to finish saving.');},
    async addResult(result,taskId){
      if(doc.nodes.some(n=>n.image_run_id===result.run_id))return;
      const task=doc.nodes.find(n=>n.id===taskId);
      if(task?.image_result_id===result.run_id)return;
      if(doc.nodes.length>=100){notify('Image saved in Project assets.');return;}
      const response=await fetch('/api/image-output/'+result.run_id);if(!response.ok)throw new Error('Image unavailable');
      const file=new File([await response.blob()],'seedream-'+result.run_id.slice(0,8)+'.png',{type:'image/png'});
      const asset=await window.VCLBridge.readMedia(file);asset.media_type='image';asset.preview_data_url=asset.data_url;delete asset.data_url;
      const node=add('image',{asset,title:asset.name,image_run_id:result.run_id},task?{x:task.x+344,y:task.y}:freePosition());
      if(task&&node){task.image_result_id=result.run_id;if(doc.edges.length<250)doc.edges.push({source:task.id,target:node.id});changed();render();}
    }
  });
  function languageLabel() {$('workspace-language').textContent=I18n.language==='zh'?'EN':'ZH';}
  $('workspace-language').addEventListener('click',()=>I18n.setLanguage(I18n.language==='zh'?'en':'zh'));
  window.addEventListener('languagechange',languageLabel);languageLabel();
  workspace(location.hash==='#canvas'?'canvas':'vcl',false);
})();
