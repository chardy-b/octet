'use strict';
(()=>{
  function panel(kind){
    const root=document.getElementById(kind+'-meters');
    if(!root)return ()=>{};
    const message=document.getElementById(kind+'-meter-note'),reset=document.getElementById('reset-'+kind+'-clips');
    const toggle=document.getElementById('toggle-'+kind+'-levels'),content=document.getElementById(kind+'-level-content');
    const preference='octet:'+kind+'-levels-collapsed';
    function collapse(collapsed){
      toggle.setAttribute('aria-expanded',String(!collapsed));
      content.hidden=collapsed;reset.hidden=collapsed;
    }
    try{collapse(localStorage.getItem(preference)==='true');}catch{/* Panels remain open when storage is unavailable. */}
    toggle.onclick=()=>{
      const collapsed=toggle.getAttribute('aria-expanded')==='true';
      collapse(collapsed);
      try{localStorage.setItem(preference,String(collapsed));}catch{/* Toggling still works without storage. */}
    };
    const rows=new Map(),history=new Map();let layout='',lastFrame=null;
    const percentage=db=>Math.max(0,Math.min(100,(db+90)/90*100));
    const number=db=>db<=-120?'−∞':db.toFixed(1).replace('-','−');
    function element(tag,cls,text){const node=document.createElement(tag);node.className=cls;if(text)node.textContent=text;return node;}
    function note(text){if(message.textContent!==text)message.textContent=text;}
    function render(frame){
      lastFrame=frame;
      const inputs=frame[kind+'s']||[],key=JSON.stringify(inputs.map(input=>[input.id,input.device,input.label]));
      if(key!==layout){
        root.replaceChildren();rows.clear();layout=key;
        for(const input of inputs){
          const card=element('div','input-meter'+(kind==='output'?' output-meter':'')),heading=element('div','input-meter-heading');
          const names=element('div','input-meter-names');names.append(element('span','input-meter-device',input.device),element('h3','',input.label));
          const badge=element('span','input-meter-badge','Waiting');heading.append(names,badge);
          const track=element('div','input-meter-track'),rms=element('span','input-meter-rms'),peak=element('span','input-meter-peak');
          track.setAttribute('role','meter');track.setAttribute('aria-label',input.device+' '+input.label+' '+kind+' level');track.setAttribute('aria-valuemin','-90');track.setAttribute('aria-valuemax','0');track.append(rms,peak);
          const scale=element('div','input-meter-scale');scale.setAttribute('aria-hidden','true');
          for(const tick of [-90,-60,-30,-12,0]){const label=element('span','',String(tick));label.style.left=percentage(tick)+'%';scale.append(label);}
          const readout=element('p','input-meter-readout');card.append(heading,track,scale,readout);root.append(card);
          rows.set(input.id,{card,badge,track,rms,peak,readout});
        }
      }
      const stale=frame.age_ms===null||frame.age_ms>1000,now=performance.now();
      for(const input of inputs){
        let saved=history.get(input.id);
        if(!saved){saved={seen:0,clipped:false,peak:-120,peakUntil:0};history.set(input.id,saved);}
        const available=input.available&&!stale;
        if(available){
          if(input.clip_seq>saved.seen)saved.clipped=true;
          saved.seen=input.clip_seq;
          if(input.peak_db>=saved.peak||now>saved.peakUntil){saved.peak=input.peak_db;saved.peakUntil=now+1000;}
        }
        const ui=rows.get(input.id),near=available&&input.peak_db>=-3;
        ui.card.classList.toggle('clipped',saved.clipped);ui.card.classList.toggle('near-clip',near);ui.card.classList.toggle('unavailable',!available);
        ui.badge.textContent=saved.clipped?'CLIP':!available?'Unavailable':near?'Near clip':input.status;
        ui.rms.style.width=available?percentage(input.rms_db)+'%':'0%';
        ui.peak.style.left=available?percentage(saved.peak)+'%':'0%';ui.peak.hidden=!available;
        ui.track.setAttribute('aria-valuenow',String(available?Math.max(-90,input.rms_db):-90));
        ui.track.setAttribute('aria-valuetext',available?`RMS ${number(input.rms_db)}, peak ${number(input.peak_db)} dBFS${saved.clipped?', clipping detected':''}`:kind+' level unavailable');
        ui.readout.textContent=available?`RMS ${number(input.rms_db)} · Peak ${number(input.peak_db)} dBFS`:(stale?'Levels unavailable · reconnecting…':input.status);
      }
      reset.disabled=![...history.values()].some(input=>input.clipped);
      note(stale&&inputs.length?'Levels unavailable · reconnecting…':frame[kind==='input'?'message':'output_message']||'Checking '+kind+' levels…');
    }
    reset.onclick=()=>{for(const saved of history.values())saved.clipped=false;if(lastFrame)render(lastFrame);};
    return render;
  }
  const inputPanel=panel('input'),outputPanel=panel('output');
  let timer=null,request=null,lastFrame=null;
  function render(frame){lastFrame=frame;inputPanel(frame);outputPanel(frame);}
  async function poll(){
    if(document.hidden)return;
    request=new AbortController();const timeout=setTimeout(()=>request?.abort(),1500);
    try{
      const response=await fetch('/api/input-levels',{signal:request.signal});if(!response.ok)throw Error('Meters unavailable');
      const frame=await response.json();if(!document.hidden)render(frame);
    }catch{
      if(!document.hidden){if(lastFrame)render({...lastFrame,age_ms:2000});else render({inputs:[],outputs:[],age_ms:null,message:'Input meters unavailable · reconnecting…',output_message:'Output meters unavailable · reconnecting…'});}
    }finally{
      clearTimeout(timeout);request=null;if(!document.hidden)timer=setTimeout(poll,100);
    }
  }
  document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(document.hidden)request?.abort();else if(!request)poll();});
  window.addEventListener('pagehide',()=>{clearTimeout(timer);request?.abort();});
  window.addEventListener('pageshow',event=>{if(event.persisted&&!request){clearTimeout(timer);poll();}});
  poll();
})();
