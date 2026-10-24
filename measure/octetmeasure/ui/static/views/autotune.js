import { api, app, bus, h, toast } from '../util.js';
import { LineChart } from '../charts.js';

export async function mount(root) {
  let closed = false, state = await api('/api/auto'), charts = [], chartRun = null;
  const header = h('div', {class:'page-head'}, h('div',{},h('h1',{text:'Auto Tune'}),
    h('p',{class:'muted',text:'Measure the selected driver pairs, choose crossovers, correct the listening area, and verify the result.'})));
  const status = h('div',{class:'card',role:'status','aria-live':'polite'});
  const controls = h('div',{class:'row'});
  const details = h('div',{class:'card'});
  const plots = h('div');
  const meter = h('p',{class:'muted',text:'Microphone: idle'});
  const editor = h('textarea',{'aria-label':'Speaker profile YAML',rows:22,spellcheck:'false',style:{width:'100%',fontFamily:'monospace'}});
  editor.value = (await api('/api/auto/profile')).yaml;
  const profile = h('details',{class:'card'},h('summary',{text:'Speaker profile and playback limits'}),
    h('p',{class:'muted',text:'Output numbers are physical channels 1–8. Confirm the wiring, limits and microphone calibration on the Pi before enabling hardware. New runs save an immutable copy of this profile.'}),editor,
    h('button',{class:'btn',text:'Save profile',onclick:async()=>{try{await api('/api/auto/profile',{method:'PUT',body:{yaml:editor.value}});toast('Profile saved');}catch(e){toast(e.message,'bad');}}}));
  root.append(header,status,meter,controls,details,plots,profile);
  async function action(name) {
    try { await api('/api/auto/'+name,{method:'POST'}); state=await api('/api/auto'); render(); }
    catch(e) { toast(e.message,'bad'); }
  }
  function button(label,act,disabled=false,danger=false) {
    return h('button',{class:'btn '+(danger?'danger':'primary'),text:label,disabled,onclick:()=>action(act)});
  }
  function render() {
    if(closed)return;
    const labels={idle:'Ready to begin',running:'Tuning in progress',await_levels:'Check amplifier levels',await_room:'Move the microphone',await_reference:'Return to the reference position',ready:'Ready to apply',applied:'Tuning active',failed:'Run needs attention',cancelled:'Playback stopped',interrupted:'Run interrupted'};
    status.replaceChildren(h('h2',{text:labels[state.status]||'Preparing'}),h('p',{text:state.message||'Place the microphone at the reference position and review the speaker profile.'}),
      h('p',{class:'muted',text:app.info.demo?'Simulation: no hardware playback.':'Audio and processing run on the Pi. You can close and reconnect this browser.'}));
    controls.replaceChildren(button('Start new run','start',state.running),button('Mute / stop','mute',false,true));
    if(state.status==='await_levels') controls.append(button('Repeat level checks','levels'));
    if(['await_levels','await_room','await_reference'].includes(state.status)) controls.append(button('Continue','continue',state.running));
    if(['failed','cancelled','interrupted'].includes(state.status)) controls.append(button('Resume saved stage','resume',state.running));
    if(state.status==='ready') controls.append(button('Apply and verify','apply',state.running));
    if(['applied','failed','interrupted'].includes(state.status)&&state.stage==='apply') controls.append(button('Restore previous configuration','rollback',state.running));
    const summary=[];
    if(state.clock_ppm!==undefined)summary.push(h('p',{text:`Measured clock offset: ${state.clock_ppm.toFixed(2)} ppm`}));
    if(state.amp_advice){
      summary.push(h('h3',{text:'Coarse amplifier matching'}));
      for(const [group,db] of Object.entries(state.amp_advice))summary.push(h('p',{text:db>1?`${group}: reduce the shared knob by approximately ${db.toFixed(1)} dB, then repeat Levels.`:`${group}: keep this setting.`}));
      summary.push(h('p',{class:'muted',text:'Estimates use each driver’s usable band at this microphone position. Final channel trims are fitted with the crossovers.'}));
    }
    if(state.level_report){
      const table=h('table',{},h('thead',{},h('tr',{},...['Driver','Amp group','Output','Mic peak'].map(x=>h('th',{text:x})))));
      const body=h('tbody');
      for(const [id,r] of Object.entries(state.level_report))body.append(h('tr',{},... [id,r.amp_group,`${r.output_db} dBFS`,`${r.peak_db.toFixed(1)} dBFS`].map(x=>h('td',{text:x}))));
      table.append(body);summary.push(table);
    }
    if(state.chosen)for(const [side,c] of Object.entries(state.chosen)) {
      const pairs=state.cfg.selected_pairs||['a','b','sub','tweeter'];
      const parts=[c.strategy==='both'?'Both exciters overlap':c.strategy==='split'?'Split exciters':'One exciter pair'];
      if(pairs.includes('sub'))parts.push(`${c.low.toFixed(0)} Hz sub / exciter`);
      if(pairs.includes('tweeter'))parts.push(`${c.high.toFixed(0)} Hz exciter / tweeter`);
      parts.push(`LR${c.order*6}`);summary.push(h('p',{text:`${side}: ${parts.join(' · ')}`}));
    }
    if(state.config_path)summary.push(h('div',{class:'row'},h('a',{class:'btn',href:'/api/auto/artifacts/camilladsp.yml',text:'Download CamillaDSP config'}),h('a',{class:'btn',href:'/api/auto/artifacts/report.json',text:'Download report'})));
    details.replaceChildren(...summary);
    details.hidden=summary.length===0;
    if(state.config_path&&chartRun!==state.id){chartRun=state.id;draw();}
  }
  async function draw(){
    const result=await api('/api/auto/results'); if(closed)return;
    charts.forEach(c=>c.destroy?.());charts=[];plots.replaceChildren();
    for(const [side,r] of Object.entries(result)){
      const box=h('div',{class:'card'},h('h2',{text:side+' · Before and after room correction'}));
      const plot=h('div');box.append(plot);plots.append(box);
      const values=[...r.before,...r.after],lo=Math.floor(Math.min(...values)/10)*10,hi=Math.ceil(Math.max(...values)/10)*10;
      charts.push(LineChart({host:plot,series:[{short:'Before',css:'var(--muted)',f:r.freq,y:r.before},{short:'After',css:'var(--accent)',f:r.freq,y:r.after}],x0:r.freq[0],x1:r.freq.at(-1),yDom:[lo,hi],yStep:10,yTitle:'Relative response (dB)',height:280}));
    }
  }
  render();
  const off=bus.on('auto_meter',e=>{meter.textContent=`Microphone peak: ${e.peak_db.toFixed(1)} dBFS`;});
  const timer=setInterval(async()=>{try{state=await api('/api/auto');render();}catch(e){if(!closed)status.textContent='Connection lost. Reconnecting to the saved run…';}},1500);
  return ()=>{closed=true;clearInterval(timer);off?.();charts.forEach(c=>c.destroy?.());};
}
