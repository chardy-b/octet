'use strict';
// The Pi owns the selected artwork; browsers are controllers or HDMI viewers.
(()=>{
  const projection=document.body.classList.contains('projection');
  const indicator=document.createElement('span');
  indicator.id='display-sync-status';indicator.setAttribute('role','status');
  document.querySelector('.visualizer-actions').append(indicator);
  let revision=null,pending=false,sending=false,dirty=false,timer,pendingMedia=null,stopped=false,lastSeek=-Infinity,selectedTheme=null;
  const preview=document.getElementById('hdmi-preview');
  let frameTimer,frameId=0,frameUrl=null,frameReceived=-Infinity,captureFrames=false,resumeFrames=()=>{};
  function updatePreviewVisibility(){
    preview.hidden=activeTheme==='off'||projection||simulating||performance.now()-frameReceived>8000;
    hdmiPreviewActive=!preview.hidden;
  }
  if(projection){
    let capturing=false,lastCapture=-Infinity;
    const copy=document.createElement('canvas');
    projectionFrameSink=source=>{
      if(capturing||!captureFrames||!revision||performance.now()-lastCapture<250||!source.width||!source.height)return;
      capturing=true;lastCapture=performance.now();
      const appliedRevision=revision,ratio=Math.min(1,480/Math.max(source.width,source.height));
      try{
        copy.width=Math.max(1,Math.round(source.width*ratio));
        copy.height=Math.max(1,Math.round(source.height*ratio));
        // Preserve the full HDMI framing so circle sizing has an accurate preview.
        copy.getContext('2d',{alpha:false}).drawImage(source,0,0,source.width,source.height,0,0,copy.width,copy.height);
        copy.toBlob(async blob=>{
          try{
            if(blob&&blob.size<=524288)await fetch('/api/control/visualizer-frame?revision='+appliedRevision,
              {method:'POST',headers:{'Content-Type':'image/jpeg'},body:blob});
          }catch{/* The settings poll reports connection health. */}
          finally{capturing=false;}
        },'image/jpeg',.8);
      }catch{capturing=false;}
    };
  }else{
    preview.onload=()=>{
      frameReceived=performance.now();updatePreviewVisibility();
    };
    preview.onerror=()=>{frameReceived=-Infinity;updatePreviewVisibility();};
    async function pollFrame(){
      if(stopped)return;
      updatePreviewVisibility();
      try{
        if(activeTheme!=='off'&&!document.hidden&&!simulating){
          const response=await fetch('/api/visualizer/frame?after='+frameId,{cache:'no-store'});
          if(response.status===200){
            const blob=await response.blob(),next=URL.createObjectURL(blob),old=frameUrl;
            frameId=Number(response.headers.get('X-Octet-Frame'));frameUrl=next;preview.src=next;
            if(old)URL.revokeObjectURL(old);
          }else if(response.status===204){
            frameReceived=performance.now();updatePreviewVisibility();
          }
        }
      }catch{/* A local preview remains available while HDMI reconnects. */}
      frameTimer=setTimeout(pollFrame,120);
    }
    resumeFrames=pollFrame;
    pollFrame();
  }
  function timeline(envelope,received){
    const s=envelope.settings;
    const epoch=envelope.animationEpoch??envelope.updated;
    return ()=>{
      const now=envelope.serverTime+Math.max(0,performance.now()-received)/1000;
      return {paletteClock:(s.paletteClock+(s.palette==='cycle'?Math.max(0,now-envelope.updated)*.75:0))%384,
        artworkTime:Math.max(0,now-epoch)*.7};
    };
  }
  function displayView(){
    const box=stage.getBoundingClientRect();
    const controls=[...document.querySelectorAll('button,select,input,a,audio,nav')].filter(element=>
      element.getClientRects().length&&getComputedStyle(element).visibility!=='hidden').length;
    return {theme:activeTheme,fullscreen:Math.abs(box.x)<1&&Math.abs(box.y)<1&&Math.abs(box.width-innerWidth)<1&&Math.abs(box.height-innerHeight)<1,
      controls,width:innerWidth,height:innerHeight,renderer:activeTheme==='turell 1'?'canvas':sceneRenderer?'webgl':'fallback'};
  }
  function snapshot(){
    const settings={theme:activeTheme,palette:paletteMode,paletteClock,halo:haloStyle,edge:edgeLight,
      response:responseMode,intensity:responseIntensity,circleSize,zoom:{...videoZooms},solar:{...solarOptions}};
    // Only an explicit theme selection may turn on a stopped display. Other
    // edits from a stale controller must not restore its previous theme.
    if(selectedTheme===null)delete settings.theme;
    else settings.theme=selectedTheme;
    const media=activeTheme==='off'?null:sceneRenderer?.mediaState();
    if(media&&Number.isFinite(media.time))settings.media={theme:activeTheme,time:media.time,paused:media.paused};
    return settings;
  }
  function syncPlayback(){
    if(!pendingMedia||pendingMedia.media.theme!==activeTheme)return;
    const state=sceneRenderer?.mediaState();
    if(!state||!Number.isFinite(state.duration)||state.duration<=0)return;
    const {media,updated,serverTime,received}=pendingMedia;
    if(state.paused!==media.paused&&!reducedMotion.matches)sceneRenderer.toggleMedia();
    // Let a decoder finish and resume presenting frames. Repeatedly seeking
    // a slow 4K decoder to a moving target can otherwise freeze its last frame.
    if(state.seeking){lastSeek=performance.now();return;}
    const elapsed=media.paused?0:Math.max(0,serverTime-updated+(performance.now()-received)/1000);
    const position=(media.time+elapsed*(state.kind==='eclipse'?solarOptions.speed:1))%state.duration;
    const difference=Math.abs(state.time-position),drift=Math.min(difference,state.duration-difference);
    if(drift>(media.paused?.04:.75)&&(media.paused||state.kind==='eclipse'||performance.now()-lastSeek>5000)){
      sceneRenderer.seekMedia(position);lastSeek=performance.now();
    }
    pendingMedia=null;syncVideoControls();
  }
  function apply(envelope){
    const received=performance.now(),s=envelope.settings;
    captureFrames=Boolean(envelope.previewRequested);
    // Refresh timing even when settings have not changed: a slow decoder,
    // background tab or stalled frame must not leave the preview out of phase.
    sharedTimeline=timeline(envelope,received);
    // The physical display's video clock already runs in real time. Repeated
    // correction seeks discard decoded frames, especially on a busy Pi.
    pendingMedia=s.media&&(!projection||envelope.revision!==revision)?
      {media:s.media,updated:envelope.updated,serverTime:envelope.serverTime,received}:null;
    if(envelope.revision===revision){syncPlayback();return;}
    lastSeek=-Infinity;
    Object.assign(solarOptions,s.solar);videoZooms={...s.zoom};
    setCircleSize(s.circleSize??1);
    responseMode=s.response;responseIntensity=s.intensity;syncResponseControls();
    haloStyle=s.halo;edgeLight=s.edge;
    document.getElementById('halo-style').value=haloStyle;
    document.getElementById('edge-light').value=String(Math.round(edgeLight*100));
    setPalette(s.palette);paletteClock=sharedTimeline().paletteClock;
    rememberPalette();refreshCycleTracks();updatePalette(0,true);
    if(activeTheme!==s.theme)setTheme(s.theme);
    updatePreviewVisibility();
    sceneRenderer?.setSolarOptions(solarOptions);
    sceneRenderer?.setMediaZoom(videoZooms[activeTheme]||1);
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
        if(state.settings.theme==='off')indicator.textContent='Visuals off';
        else if(!projection)indicator.textContent=simulating?'Local sample preview':!preview.hidden?'Live HDMI preview':
          state.display.online?'Connecting HDMI preview…':'Local preview · HDMI offline';
      }catch{indicator.textContent='HDMI link reconnecting…';}
    }
    syncPlayback();timer=setTimeout(poll,1000);
  }
  async function publish(){
    pending=false;
    if(sending||projection)return;
    sending=true;dirty=false;
    const selection=selectedTheme,settings=snapshot();selectedTheme=null;
    try{
      const response=await fetch('/api/control/visualizer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings)});
      if(!response.ok)throw Error('Display offline');
      const state=await response.json();
      // The controller adopts the same server timestamp as the HDMI browser.
      // An edit made during this request is published next and must survive.
      if(!dirty)apply(state);
      indicator.textContent=state.settings.theme==='off'?'Visuals off':'Updating HDMI display…';
    }catch{selectedTheme=selectedTheme??selection;dirty=true;indicator.textContent='HDMI link reconnecting…';}
    finally{sending=false;if(dirty){pending=true;setTimeout(publish,500);}}
  }
  const controls=new Set(['theme-select','immersive-theme-select','palette-select','immersive-palette',
    'palette-scrub','immersive-palette-scrub','palette-play','immersive-palette-play','halo-style','edge-light','circle-size','immersive-circle-size',
    'response-mode','immersive-response-mode','response-intensity','immersive-response-intensity',
    'video-scrub','immersive-video-scrub','video-play','immersive-video-play','video-zoom','immersive-video-zoom',
    'solar-mode','immersive-solar-mode','solar-treatment','immersive-solar-treatment','solar-speed','immersive-solar-speed',
    'solar-sun','immersive-solar-sun','solar-diamond','immersive-solar-diamond','solar-totality-jump','immersive-solar-totality-jump']);
  let editTimer;
  function edit(event){
    if(projection||(!controls.has(event.target.id)&&!event.target.closest?.('.palette-card')))return;
    if(event.type==='change'&&['theme-select','immersive-theme-select'].includes(event.target.id))selectedTheme=activeTheme;
    sharedTimeline=null;dirty=true;pending=true;pendingMedia=null;clearTimeout(editTimer);editTimer=setTimeout(publish,120);
  }
  for(const event of ['change','input','click'])document.addEventListener(event,edit);
  window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer);clearTimeout(editTimer);clearTimeout(frameTimer);if(frameUrl){URL.revokeObjectURL(frameUrl);frameUrl=null;}frameId=0;frameReceived=-Infinity;preview.hidden=true;hdmiPreviewActive=false;});
  window.addEventListener('pageshow',event=>{if(event.persisted){stopped=false;poll();resumeFrames();}});
  poll();
})();
