'use strict';
(() => {
  const el = id => document.getElementById(id), ns = 'http://www.w3.org/2000/svg';
  let result = null, loading = false, requestId = 0;
  const svgNode = (tag, attrs = {}, text) => {const node=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);if(text!==undefined)node.textContent=text;return node;};
  const hz = value => value >= 1000 ? `${(value/1000).toLocaleString(undefined,{maximumFractionDigits:1})}k` : String(Math.round(value));
  function chart(parent, title, freq, series, phase = false) {
    const figure=document.createElement('figure');figure.className='measurement-chart';
    const caption=document.createElement('figcaption');caption.textContent=title;figure.append(caption);
    const width=760,height=290,left=58,right=18,top=16,bottom=44;
    const lo=Math.max(10,Math.min(...freq)),hi=Math.max(...freq),span=Math.log10(hi/lo);
    const finite=series.flatMap(s=>s.values.filter(Number.isFinite));
    let ymin=phase?-180:Math.floor(Math.min(...finite)/5)*5-5, ymax=phase?180:Math.ceil(Math.max(...finite)/5)*5+5;
    if(!finite.length){ymin=-30;ymax=10;}
    const x=f=>left+Math.log10(f/lo)/span*(width-left-right), y=v=>top+(ymax-v)/(ymax-ymin)*(height-top-bottom);
    const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':title,tabindex:'0'});
    svg.append(svgNode('title',{},title),svgNode('desc',{},'Use left and right arrow keys to inspect the curves. Frequency uses a logarithmic axis.'));
    const yticks=phase?[-180,-90,0,90,180]:Array.from({length:6},(_,i)=>ymin+(ymax-ymin)*i/5);
    for(const value of yticks){svg.append(svgNode('line',{x1:left,x2:width-right,y1:y(value),y2:y(value),class:'measurement-gridline'}),svgNode('text',{x:left-9,y:y(value)+4,'text-anchor':'end',class:'measurement-axis'},`${Math.round(value)}${phase?'°':''}`));}
    for(const value of [20,30,60,100,200,300,500,1000,2000,3000,5000,10000,20000].filter(f=>f>=lo&&f<=hi)){
      svg.append(svgNode('line',{x1:x(value),x2:x(value),y1:top,y2:height-bottom,class:'measurement-gridline'}),svgNode('text',{x:x(value),y:height-bottom+19,'text-anchor':'middle',class:'measurement-axis'},hz(value)));
    }
    svg.append(svgNode('text',{x:width/2,y:height-5,'text-anchor':'middle',class:'measurement-axis'},'Frequency (Hz)'),svgNode('text',{x:left,y:12,class:'measurement-axis'},phase?'Phase (degrees)':'Relative level (dB)'));
    for(const s of series){
      let segment=[];
      const flush=()=>{if(segment.length>1)svg.append(svgNode('polyline',{points:segment.join(' '),fill:'none',stroke:s.color,'stroke-width':'2','stroke-dasharray':s.dash||'','vector-effect':'non-scaling-stroke'}));segment=[];};
      for(let i=0;i<freq.length;i++){
        const v=s.values[i];if(!Number.isFinite(v)){flush();continue;}
        if(phase&&i>0&&Math.abs(v-s.values[i-1])>180)flush();
        segment.push(`${x(freq[i]).toFixed(2)},${y(v).toFixed(2)}`);
      }
      flush();
    }
    const cursor=svgNode('line',{x1:left,x2:left,y1:top,y2:height-bottom,class:'measurement-cursor',visibility:'hidden'});svg.append(cursor);
    const reading=document.createElement('output');reading.className='measurement-reading';reading.textContent='Point or use arrow keys to inspect values.';
    let index=0;
    function inspect(i){index=Math.max(0,Math.min(freq.length-1,i));cursor.setAttribute('x1',x(freq[index]));cursor.setAttribute('x2',x(freq[index]));cursor.setAttribute('visibility','visible');reading.textContent=`${Math.round(freq[index])} Hz · `+series.map(s=>`${s.label}: ${Number.isFinite(s.values[index])?s.values[index].toFixed(1)+(phase?'°':' dB'):'unavailable'}`).join(' · ');}
    svg.addEventListener('pointermove',event=>{const rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)/rect.width*width;const f=lo*10**((px-left)/(width-left-right)*span);let nearest=0;for(let i=1;i<freq.length;i++)if(Math.abs(Math.log(freq[i]/f))<Math.abs(Math.log(freq[nearest]/f)))nearest=i;inspect(nearest);});
    svg.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();inspect(index+(event.key==='ArrowRight'?1:-1));}});
    svg.addEventListener('focus',()=>inspect(index));
    const legend=document.createElement('ul');legend.className='measurement-legend';
    for(const s of series){const li=document.createElement('li');const swatch=svgNode('svg',{viewBox:'0 0 24 8','aria-hidden':'true'});swatch.append(svgNode('line',{x1:0,x2:24,y1:4,y2:4,stroke:s.color,'stroke-width':2,'stroke-dasharray':s.dash||''}));li.append(swatch,document.createTextNode(s.label));legend.append(li);}
    figure.append(svg,legend,reading);parent.append(figure);
  }
  function driverCharts() {
    const root=el('measurement-driver-charts');root.replaceChildren();
    const d=result?.drivers.find(driver=>driver.id===el('measurement-driver').value);
    if(!d)return;
    el('measurement-settings').textContent=`Output ${d.output} · ${d.delay_ms.toFixed(2)} ms delay · ${d.polarity<0?'inverted':'normal'} polarity · ${d.attenuation_db.toFixed(1)} dB relative attenuation`;
    chart(root,'Driver frequency response',d.freq,[{label:'Measured driver',values:d.measured,color:'#bade85'},{label:'Predicted with DSP',values:d.predicted,color:'#81bddb'}]);
    chart(root,'Driver phase',d.freq,[{label:'Measured driver',values:d.phase,color:'#bade85'},{label:'Predicted with DSP',values:d.predicted_phase,color:'#81bddb'}],true);
  }
  function render(data) {
    result=data;
    el('measurement-title').textContent=`Measurements · ${data.profile.name}`;
    el('measurement-status').textContent=data.message;
    el('measurement-notes').textContent=data.notes||'';
    const systems=el('measurement-systems');systems.replaceChildren();
    for(const s of data.systems||[]){
      const pairs=s.crossover.active_pairs||['a','b','sub','tweeter'];
      const crossovers=[pairs.includes('sub')?`${Math.round(s.crossover.low)} Hz sub / exciter`:null,pairs.includes('tweeter')?`${Math.round(s.crossover.high)} Hz exciter / tweeter`:null].filter(Boolean);
      chart(systems,`${s.side==='L'?'Left':'Right'} system${crossovers.length?' · '+crossovers.join(' · '):' · exciter pair only'}`,s.freq,[
      {label:'Before room EQ',values:s.before,color:'#9aab9e'},
      {label:'Final measured',values:s.after,color:'#bade85'},
      {label:'Target shape',values:s.target,color:'#d8b884',dash:'5 5'}]);}
    const select=el('measurement-driver');select.replaceChildren();
    for(const d of data.drivers||[])select.add(new Option(`${d.output}. ${d.model} · ${d.side==='L'?'left':'right'}`,d.id));
    el('measurement-driver-controls').hidden=!(data.drivers||[]).length;
    el('measurement-driver-charts').replaceChildren();el('measurement-settings').textContent='';
    driverCharts();
  }
  function update() {el('auto-view').disabled=loading||!el('auto-profiles').value;}
  async function show() {
    const id=el('auto-profiles').value;if(!id)return;
    const serial=++requestId;loading=true;update();
    el('profile-measurements').hidden=false;
    el('measurement-title').textContent=`Measurements · ${el('auto-profiles').selectedOptions[0].textContent}`;
    el('measurement-status').textContent='Loading saved measurements…';
    el('measurement-systems').replaceChildren();el('measurement-driver-charts').replaceChildren();el('measurement-driver-controls').hidden=true;el('measurement-notes').textContent='';
    try{
      const response=await fetch('/api/auto/profiles/'+encodeURIComponent(id)+'/measurements');const data=await response.json();
      if(!response.ok)throw Error(data.error||'Could not load measurements');
      if(serial===requestId)render(data);
    }catch(error){if(serial===requestId)el('measurement-status').textContent=error.message;}
    finally{if(serial===requestId){loading=false;update();}}
  }
  el('auto-view').onclick=show;
  el('measurement-close').onclick=()=>{requestId++;loading=false;el('profile-measurements').hidden=true;update();};
  el('measurement-driver').onchange=driverCharts;
  el('auto-profiles').addEventListener('change',()=>{update();if(!el('profile-measurements').hidden)show();});
  window.profileMeasurementViewer={update};
})();
