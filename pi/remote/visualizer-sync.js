'use strict';
// The Pi owns the selected artwork; browsers are controllers or HDMI viewers.
(()=>{
  const projection=document.body.classList.contains('projection');
  const indicator=document.createElement('span');
  indicator.id='display-sync-status';indicator.setAttribute('role','status');
  document.querySelector('.visualizer-actions').append(indicator);
  let revision=null,pending=false,sending=false,dirty=false,timer,pendingMedia=null,stopped=false;
  function displayView(){
    const box=stage.getBoundingClientRect();
    const controls=[...document.querySelectorAll('button,select,input,a,audio,nav')].filter(element=>
      element.getClientRects().length&&getComputedStyle(element).visibility!=='hidden').length;
    return {theme:activeTheme,fullscreen:Math.abs(box.x)<1&&Math.abs(box.y)<1&&Math.abs(box.width-innerWidth)<1&&Math.abs(box.height-innerHeight)<1,
      controls,width:innerWidth,height:innerHeight,renderer:activeTheme==='turell 1'?'canvas':sceneRenderer?'webgl':'fallback'};
  }
  function snapshot(){
    const settings={theme:activeTheme,palette:paletteMode,paletteClock,halo:haloStyle,edge:edgeLight,
      response:responseMode,intensity:responseIntensity,zoom:{...videoZooms},solar:{...solarOptions}};
    const media=sceneRenderer?.mediaState();
    if(media&&Number.isFinite(media.time))settings.media={theme:activeTheme,time:media.time,paused:media.paused};
    return settings;
  }
  function syncPlayback(){
    if(!pendingMedia||pendingMedia.media.theme!==activeTheme)return;
    const state=sceneRenderer?.mediaState();
    if(!state||!Number.isFinite(state.duration)||state.duration<=0)return;
    const {media,updated,serverTime,received}=pendingMedia;
    const elapsed=media.paused?0:Math.max(0,serverTime-updated+(Date.now()-received)/1000);
    const position=(media.time+elapsed*(state.kind==='eclipse'?solarOptions.speed:1))%state.duration;
    if(Math.abs(state.time-position)>.3)sceneRenderer.seekMedia(position);
    if(state.paused!==media.paused&&!reducedMotion.matches)sceneRenderer.toggleMedia();
    pendingMedia=null;syncVideoControls();
  }
  function apply(envelope){
    if(envelope.revision===revision)return;
    const s=envelope.settings,elapsed=Math.max(0,envelope.serverTime-envelope.updated);
    Object.assign(solarOptions,s.solar);videoZooms={...s.zoom};
    responseMode=s.response;responseIntensity=s.intensity;syncResponseControls();
    haloStyle=s.halo;edgeLight=s.edge;
    document.getElementById('halo-style').value=haloStyle;
    document.getElementById('edge-light').value=String(Math.round(edgeLight*100));
    setPalette(s.palette);paletteClock=(s.paletteClock+(s.palette==='cycle'?elapsed:0))%384;
    rememberPalette();refreshCycleTracks();updatePalette(0,true);
    if(activeTheme!==s.theme)setTheme(s.theme);
    sceneRenderer?.setSolarOptions(solarOptions);
    sceneRenderer?.setMediaZoom(videoZooms[activeTheme]||1);
    pendingMedia=s.media?{media:s.media,updated:envelope.updated,serverTime:envelope.serverTime,received:Date.now()}:null;
    syncPlayback();revision=envelope.revision;
  }
  async function poll(){
    if(stopped)return;
    if(!pending&&!sending&&!dirty){
      try{
        const response=await fetch('/api/visualizer/settings'+(projection?'?display=1&revision='+(revision||'')+'&view='+encodeURIComponent(JSON.stringify(displayView())):''),{cache:'no-store'});
        if(!response.ok)throw Error('Display offline');
        const state=await response.json();
        // A local edit made while fetching always wins this round.
        if(!dirty&&!sending)apply(state);
        indicator.textContent=state.display.online?(state.display.revision===state.revision?'HDMI display linked':'Updating HDMI display…'):'Waiting for HDMI display';
      }catch{indicator.textContent='HDMI link reconnecting…';}
    }
    syncPlayback();timer=setTimeout(poll,1000);
  }
  async function publish(){
    pending=false;
    if(sending||projection)return;
    sending=true;dirty=false;
    try{
      const response=await fetch('/api/control/visualizer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(snapshot())});
      if(!response.ok)throw Error('Display offline');
      const state=await response.json();revision=state.revision;
      indicator.textContent='Updating HDMI display…';
    }catch{dirty=true;indicator.textContent='HDMI link reconnecting…';}
    finally{sending=false;if(dirty){pending=true;setTimeout(publish,500);}}
  }
  const controls=new Set(['theme-select','immersive-theme-select','palette-select','immersive-palette',
    'palette-scrub','immersive-palette-scrub','palette-play','immersive-palette-play','halo-style','edge-light',
    'response-mode','immersive-response-mode','response-intensity','immersive-response-intensity',
    'video-scrub','immersive-video-scrub','video-play','immersive-video-play','video-zoom','immersive-video-zoom',
    'solar-mode','immersive-solar-mode','solar-treatment','immersive-solar-treatment','solar-speed','immersive-solar-speed',
    'solar-sun','immersive-solar-sun','solar-diamond','immersive-solar-diamond','solar-totality-jump','immersive-solar-totality-jump']);
  let editTimer;
  function edit(event){
    if(projection||(!controls.has(event.target.id)&&!event.target.closest?.('.palette-card')))return;
    dirty=true;pending=true;pendingMedia=null;clearTimeout(editTimer);editTimer=setTimeout(publish,120);
  }
  for(const event of ['change','input','click'])document.addEventListener(event,edit);
  window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer);clearTimeout(editTimer);});
  window.addEventListener('pageshow',event=>{if(event.persisted){stopped=false;poll();}});
  poll();
})();
