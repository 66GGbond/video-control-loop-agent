/* Pure graph and viewport operations shared by the browser and Node checks. */
(function(root) {
  const width=292, height=254;
  const clone=doc=>({...doc,viewport:{...doc.viewport},nodes:doc.nodes.map(n=>({...n})),edges:doc.edges.map(e=>({...e}))});
  function ancestors(doc,target) {
    const seen=new Set(), visiting=new Set(), ordered=[];
    function walk(id) {
      if(visiting.has(id)) throw new Error('Connections cannot form a cycle.');
      if(seen.has(id))return;
      visiting.add(id);
      doc.edges.filter(e=>e.target===id).forEach(e=>walk(e.source));
      visiting.delete(id);seen.add(id);
      const node=doc.nodes.find(n=>n.id===id);if(node)ordered.push(node);
    }
    walk(target);return ordered;
  }
  function canConnect(doc,source,target) {
    return source!==target && doc.nodes.some(n=>n.id===source) && doc.nodes.some(n=>n.id===target) &&
      !doc.edges.some(e=>e.source===source&&e.target===target) && !ancestors(doc,source).some(n=>n.id===target);
  }
  function bundle(doc,target) {
    const nodes=ancestors(doc,target), names=new Map(),assets=[];
    if(nodes.some(n=>n.kind==='output')) throw new Error('Generated videos can be previewed here. Add a public video URL to use one as a reference.');
    for(const node of nodes.filter(n=>n.asset)) {
      const key=node.asset.name.toLowerCase(), prior=names.get(key);
      if(prior && JSON.stringify(prior)!==JSON.stringify(node.asset))throw new Error('Reference filenames must be unique.');
      if(!prior){names.set(key,node.asset);assets.push(node.asset);}
    }
    const text=nodes.filter(n=>n.kind==='text'||n.kind==='task').map(n=>n.text.trim()).filter(Boolean).join('\n\n');
    if(!text)throw new Error('Add a brief before sending to the Agent.');
    const mentions=assets.map(a=>'@'+a.name).filter(name=>!text.includes(name));
    return {text:text+(mentions.length?'\n\n'+mentions.join(' '):''),assets};
  }
  function point(x,y,viewport){return {x:(x-viewport.x)/viewport.zoom,y:(y-viewport.y)/viewport.zoom};}
  function zoomAt(viewport,zoom,x,y){const p=point(x,y,viewport);zoom=Math.max(.2,Math.min(2,zoom));return {x:x-p.x*zoom,y:y-p.y*zoom,zoom};}
  function fit(nodes,w,h){
    if(!nodes.length)return {x:Math.max(80,w/2-146),y:Math.max(90,h/2-127),zoom:1};
    const x=Math.min(...nodes.map(n=>n.x)), y=Math.min(...nodes.map(n=>n.y));
    const right=Math.max(...nodes.map(n=>n.x+width)),bottom=Math.max(...nodes.map(n=>n.y+height));
    const zoom=Math.max(.2,Math.min(1.15,(w-160)/(right-x),(h-190)/(bottom-y)));
    return {x:(w-(right-x)*zoom)/2-x*zoom,y:(h-(bottom-y)*zoom)/2-y*zoom,zoom};
  }
  const api={width,height,clone,ancestors,canConnect,bundle,point,zoomAt,fit};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.CanvasCore=api;
})(typeof window==='undefined'?globalThis:window);
