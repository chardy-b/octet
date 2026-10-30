'use strict';
const stage = document.getElementById('visualizer-stage');
const canvas = document.getElementById('visualizer-canvas');
const context = canvas.getContext('2d', {alpha: false});
const statusLabel = document.getElementById('signal-status');
const notice = document.getElementById('visualizer-notice');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const clamp = (value, min = 0, max = 1) => Math.max(min, Math.min(max, value));
let feed = null, lastSignal = -Infinity, sourceActive = false;
let targetEnergy = 0, targetPeak = 0, energy = 0, peak = 0;
let haloEnergy = 0, haloPeak = 0;
let width = 0, height = 0, previousTime = 0, animation = 0;
let controlsTimer;
let simulating = false, paletteMode = 'cycle', paletteClock = 0, haloStyle = 'curated', edgeLight = .35;
const spectrum = [0,0,0,.3];
let responseMode='combined',responseIntensity='balanced';
const responseModes={original:'The original, gently smoothed halo.',bloom:'Bass opens and brightens the soft halo.',depth:'Musical dynamics deepen the center gradient.',rim:'A slow breath of light along the outer edge.',shimmer:'Treble lights a fine, soft ring around the center.',combined:'Bass bloom, gradient depth, and outer-rim breathing together.'};
const responseGains={subtle:.5,balanced:1,strong:1.6};
const responseEnvelope={bass:0,body:0,rim:0,treble:0};
let analysisBands=[0,0,0,0];
try{
  const mode=localStorage.getItem('octet.visualizer.response'),intensity=localStorage.getItem('octet.visualizer.intensity');
  if(Object.hasOwn(responseModes,mode))responseMode=mode;
  if(Object.hasOwn(responseGains,intensity))responseIntensity=intensity;
}catch{/* Optional preference. */}
function responseProfile(mode=responseMode,intensity=responseIntensity){
  const gain=responseGains[intensity]||1,motion=reducedMotion.matches?.18:1;
  const enabled=key=>mode===key||(mode==='combined'&&key!=='shimmer');
  return {bloom:enabled('bloom')?responseEnvelope.bass*gain*motion:0,
    depth:enabled('depth')?responseEnvelope.body*gain*motion:0,
    rim:enabled('rim')?responseEnvelope.rim*gain*motion:0,
    shimmer:enabled('shimmer')?responseEnvelope.treble*gain*motion:0};
}
function syncResponseControls(){
  for(const prefix of ['', 'immersive-']){
    document.getElementById(prefix+'response-mode').value=responseMode;
    document.getElementById(prefix+'response-intensity').value=responseIntensity;
  }
  document.getElementById('response-description').textContent=responseModes[responseMode];
  document.getElementById('response-source-note').textContent=simulating?'Local music uses separate bass and treble analysis.':'Live Pi audio currently follows overall dynamics; use preview music to compare frequency responses.';
}
for(const prefix of ['', 'immersive-'])for(const [suffix,choices] of [['mode',responseModes],['intensity',responseGains]]){
  document.getElementById(prefix+'response-'+suffix).onchange=event=>{
    if(!Object.hasOwn(choices,event.target.value))return;
    if(suffix==='mode')responseMode=event.target.value;else responseIntensity=event.target.value;
    try{localStorage.setItem('octet.visualizer.response',responseMode);localStorage.setItem('octet.visualizer.intensity',responseIntensity);}catch{/* Optional preference. */}
    syncResponseControls();showControls();
  };
}
syncResponseControls();
const sampleAudio = document.getElementById('simulation-audio');
const localSimulation = OctetSimulation.create(sampleAudio, sampleState, message=>{
  notice.textContent=message;
  statusLabel.textContent='Sample music unavailable';
  targetEnergy=targetPeak=0;
});
let activeTheme = 'off', sceneRenderer = null, rendererAttempted = false, artworkTime = 0;
// Installed by visualizer-sync.js while linked to the Pi. Audio envelopes use
// bounded frame steps, but shared color/motion clocks follow elapsed real time.
let sharedTimeline = null;
let projectionFrameSink = null;
let hdmiPreviewActive = false;
const sceneCanvas = document.getElementById('scene-canvas');
const themeDetails = {off:['Visuals off','Choose a theme to turn on HDMI visuals.'],...OctetThemes.descriptions};
// The Pi owns the selection. A stale local preference must never turn it on.

// Blend in OKLab to keep slowly changing light perceptually even.
function toLab(rgb) {
  const [r, g, b] = rgb.map(value => {
    const v = value / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  const l = Math.cbrt(0.4122214708*r + 0.5363325363*g + 0.0514459929*b);
  const m = Math.cbrt(0.2119034982*r + 0.6806995451*g + 0.1073969566*b);
  const s = Math.cbrt(0.0883024619*r + 0.2817188376*g + 0.6299787005*b);
  return [0.2104542553*l + 0.793617785*m - 0.0040720468*s,
    1.9779984951*l - 2.428592205*m + 0.4505937099*s,
    0.0259040371*l + 0.7827717662*m - 0.808675766*s];
}
function lightRGB(a, b, amount=0) {
  const c = a.map((v, i) => v + (b[i] - v) * amount);
  const l = (c[0] + 0.3963377774*c[1] + 0.2158037573*c[2]) ** 3;
  const m = (c[0] - 0.1055613458*c[1] - 0.0638541728*c[2]) ** 3;
  const s = (c[0] - 0.0894841775*c[1] - 1.291485548*c[2]) ** 3;
  return [4.0767416621*l - 3.3077115913*m + 0.2309699292*s,
    -1.2684380046*l + 2.6097574011*m - 0.3413193965*s,
    -0.0041960863*l - 0.7034186147*m + 1.707614701*s].map(value => {
    const v = clamp(value);
    return (v <= 0.0031308 ? 12.92*v : 1.055*v ** (1/2.4) - 0.055) * 255;
  });
}
const lightColor=(a,b=a,amount=0)=>'rgb('+lightRGB(a,b,amount).map(value=>value.toFixed(3)).join(',')+')';
// Digital studies of documented Skyspace relationships, not certified LED
// settings. See README for UT Austin, the Albers Foundation, and W3C sources.
const paletteDefinitions=[
  {id:'lime',name:'Lime / periwinkle',field:[143,177,35],inner:[111,133,185],halo:[255,210,126]},
  {id:'magenta',name:'Magenta / periwinkle',field:[195,27,243],inner:[91,143,248],halo:[155,112,255],note:'A luminous violet halo connects cool blue to saturated magenta.'},
  {id:'ultraviolet',name:'Magenta / ultraviolet',field:[151,0,160],inner:[112,0,246],core:[175,0,255],halo:[172,61,226],note:'A violet halo keeps the hot magenta core and electric-violet rim in the same family.'},
  {id:'lagoon',name:'Lagoon / orchid',field:[0,147,133],inner:[190,135,244],halo:[136,101,230],note:'A violet transition keeps the orchid center distinct from the green-blue field.'},
  {id:'watermelon',name:'Watermelon / teal',field:[219,70,111],inner:[66,174,178],halo:[250,174,155]},
  {id:'violet',name:'Violet / apricot',field:[116,78,163],inner:[237,176,119],halo:[243,179,153]},
  {id:'indigo',name:'Indigo / amber',field:[65,78,148],inner:[226,168,67],halo:[247,212,148]},
  {id:'rose',name:'Rose / celadon',field:[207,115,157],inner:[155,196,164],halo:[244,201,188]},
  {id:'ochre',name:'Ochre / lavender',field:[192,155,57],inner:[174,154,207],halo:[245,214,156]},
  {id:'cyan',name:'Cyan / vermilion',field:[55,158,171],inner:[225,106,81],halo:[249,192,153]},
  {id:'plum',name:'Plum / pale lemon',field:[107,64,130],inner:[215,221,137],halo:[229,185,191]},
  {id:'pearl',name:'Pearl / midnight',field:[191,192,175],inner:[47,68,114],halo:[240,225,190]},
].map(spec=>{
  const fieldLab=toLab(spec.field),innerLab=toLab(spec.inner);
  return {...spec,fieldLab,innerLab,haloLab:toLab(spec.halo),
    coreLab:spec.core?toLab(spec.core):[clamp(innerLab[0]+.055),innerLab[1]*.88+fieldLab[1]*.12,innerLab[2]*.88+fieldLab[2]*.12],
    edgeLab:[clamp(fieldLab[0]+.10),fieldLab[1]*.88+innerLab[1]*.12,fieldLab[2]*.88+innerLab[2]*.12]};
});
const colorRoles=['field','inner','halo','edge','core'];
const currentColors=Object.fromEntries(colorRoles.map(key=>[key,paletteDefinitions[0][key+'Lab'].slice()]));
let circleSize=1;
try{
  const saved=localStorage.getItem('octet.visualizer.circle-size');
  if(saved!==null&&Number.isFinite(Number(saved)))circleSize=clamp(Number(saved),.25,2);
}catch{/* Optional framing preference. */}
const shaderPalette={field:[],halo:[],edge:[],edgeLight,circleSize};
function syncCircleControls(){
  const percent=String(Math.round(circleSize*100)),circular=!['well','overlap'].includes(activeTheme);
  for(const prefix of ['','immersive-']){
    document.getElementById(prefix+'circle-sizing').hidden=!circular;
    document.getElementById(prefix+'circle-size').value=percent;
    document.getElementById(prefix+'circle-size-value').textContent=percent+'%';
  }
}
function setCircleSize(value){
  if(!Number.isFinite(value))return;
  circleSize=clamp(value,.25,2);shaderPalette.circleSize=circleSize;
  try{localStorage.setItem('octet.visualizer.circle-size',String(circleSize));}catch{/* Optional preference. */}
  syncCircleControls();
}
for(const prefix of ['','immersive-'])document.getElementById(prefix+'circle-size').oninput=event=>{
  setCircleSize(Number(event.target.value)/100);showControls();
};
syncCircleControls();
try{
  const saved=localStorage.getItem('octet.visualizer.palette');
  if(['cycle','scrub'].includes(saved)||paletteDefinitions.some(spec=>spec.id===saved))paletteMode=saved;
  const clock=Number(localStorage.getItem('octet.visualizer.palette-position'));
  if(Number.isFinite(clock))paletteClock=clamp(clock,0,paletteDefinitions.length*32);
  const style=localStorage.getItem('octet.visualizer.halo');if(['curated','bridge','contrast'].includes(style))haloStyle=style;
  const edge=localStorage.getItem('octet.visualizer.edge');if(edge!==null&&Number.isFinite(Number(edge)))edgeLight=clamp(Number(edge));
}catch{/* Default color cycle. */}
if(!['cycle','scrub'].includes(paletteMode))paletteClock=paletteDefinitions.findIndex(spec=>spec.id===paletteMode)*32;
for(const id of ['palette-select','immersive-palette']){
  const select=document.getElementById(id);
  for(const spec of paletteDefinitions){const option=document.createElement('option');option.value=spec.id;option.textContent=spec.name;select.append(option);}
  select.value=paletteMode;
}
const paletteButtons=paletteDefinitions.map(spec=>{
  const button=document.createElement('button'),preview=document.createElement('span'),label=document.createElement('span');
  button.type='button';button.className='palette-card';button.setAttribute('aria-label',spec.name);
  preview.className='palette-orbit';preview.setAttribute('aria-hidden','true');
  preview.style.background=`radial-gradient(circle,${lightColor(spec.coreLab)} 0 5%,rgb(${spec.inner}) 18%,rgb(${spec.halo}) 24%,rgb(${spec.field}) 45% 80%,${lightColor(spec.edgeLab)} 100%)`;
  label.textContent=spec.name;button.append(preview,label);button.onclick=()=>setPalette(spec.id);
  document.getElementById('palette-swatches').append(button);return button;
});
function rememberPalette(){
  try{
    localStorage.setItem('octet.visualizer.palette',paletteMode);
    localStorage.setItem('octet.visualizer.palette-position',String(paletteClock));
    localStorage.setItem('octet.visualizer.halo',haloStyle);
    localStorage.setItem('octet.visualizer.edge',String(edgeLight));
  }catch{/* Optional preference. */}
}
function setPalette(mode){
  if(!['cycle','scrub'].includes(mode)&&!paletteDefinitions.some(spec=>spec.id===mode))return;
  paletteMode=mode;
  if(!['cycle','scrub'].includes(mode))paletteClock=paletteDefinitions.findIndex(spec=>spec.id===mode)*32;
  document.getElementById('palette-select').value=mode;
  document.getElementById('immersive-palette').value=mode;
  rememberPalette();updatePalette(0,true);
}
document.getElementById('palette-select').onchange=event=>setPalette(event.target.value);
document.getElementById('immersive-palette').onchange=event=>{setPalette(event.target.value);showControls();};
for(const id of ['palette-scrub','immersive-palette-scrub']){
  const input=document.getElementById(id);input.max=String(paletteDefinitions.length*1000);
  input.oninput=event=>{
    const value=Number(event.target.value);if(!Number.isFinite(value))return;
    paletteClock=clamp(value,0,paletteDefinitions.length*1000)/1000*32;
    setPalette('scrub');showControls();
  };
}
for(const id of ['palette-play','immersive-palette-play'])document.getElementById(id).onclick=()=>{setPalette(paletteMode==='cycle'?'scrub':'cycle');showControls();};
document.getElementById('halo-style').value=haloStyle;
document.getElementById('halo-style').onchange=event=>{
  if(!['curated','bridge','contrast'].includes(event.target.value))return;
  haloStyle=event.target.value;rememberPalette();refreshCycleTracks();updatePalette(0,true);
};
document.getElementById('edge-light').value=String(Math.round(edgeLight*100));
document.getElementById('edge-light').oninput=event=>{
  const value=Number(event.target.value);if(!Number.isFinite(value))return;
  edgeLight=clamp(value/100);rememberPalette();updatePalette(0,true);
};
function cycleColorsAt(position){
  const wrapped=((position%paletteDefinitions.length)+paletteDefinitions.length)%paletteDefinitions.length;
  const index=Math.floor(wrapped),a=paletteDefinitions[index],b=paletteDefinitions[(index+1)%paletteDefinitions.length];
  const phase=clamp(((wrapped%1)-.30)/.70),blend=phase*phase*(3-2*phase);
  const colors=Object.fromEntries(colorRoles.map(key=>[key,a[key+'Lab'].map((value,i)=>value+(b[key+'Lab'][i]-value)*blend)]));
  if(haloStyle==='bridge')colors.halo=[clamp(Math.max(colors.field[0],colors.inner[0])+.12),(colors.field[1]+colors.inner[1])*.5,(colors.field[2]+colors.inner[2])*.5];
  if(haloStyle==='contrast')colors.halo=[.84,-colors.inner[1]*.75,-colors.inner[2]*.75];
  return {a,b,index,blend,colors};
}
function refreshCycleTracks(){
  for(const key of ['field','halo','inner']){
    const stops=[];
    for(let i=0;i<=paletteDefinitions.length*12;i++){
      const position=i/12;
      stops.push(lightColor(cycleColorsAt(position).colors[key])+' '+(position/paletteDefinitions.length*100).toFixed(3)+'%');
    }
    for(const id of ['palette-panel','immersive-color-tools'])document.getElementById(id).style.setProperty('--'+key+'-track','linear-gradient(90deg,'+stops.join(',')+')');
  }
}
refreshCycleTracks();
let lastPaletteUi=-Infinity;
function updatePalette(dt,forceUi=false){
  if(paletteMode==='cycle')paletteClock=(paletteClock+dt*(reducedMotion.matches?0:.75+haloEnergy*.25))%(32*paletteDefinitions.length);
  const {a,b,index,blend,colors}=cycleColorsAt(paletteClock/32);
  for(const key of colorRoles)currentColors[key]=colors[key].map((value,i)=>value+(i===0&&simulating&&paletteMode==='cycle'?(spectrum[3]-.3)*.018:0));
  for(const key of ['field','halo','edge'])shaderPalette[key]=lightRGB(currentColors[key],currentColors[key]).map(v=>v/255);
  shaderPalette.edgeLight=edgeLight;
  const caption=blend>.02?a.name+' → '+b.name:a.name;
  if(document.getElementById('palette-caption').textContent!==caption)document.getElementById('palette-caption').textContent=caption;
  // Immediate feedback on input; otherwise bound DOM work below render cadence.
  if(!forceUi&&performance.now()-lastPaletteUi<100)return;
  lastPaletteUi=performance.now();
  const percent=clamp(paletteClock/(32*paletteDefinitions.length))*100;
  for(const id of ['palette-scrub','immersive-palette-scrub']){
    const input=document.getElementById(id);input.value=String(Math.round(paletteClock/32*1000));
    input.setAttribute('aria-valuetext',Math.round(percent)+'% · '+caption);
  }
  for(const id of ['palette-panel','immersive-color-tools'])document.getElementById(id).style.setProperty('--cycle-position',percent+'%');
  for(const id of ['palette-play','immersive-palette-play'])document.getElementById(id).textContent=paletteMode==='cycle'?'Pause cycle':'Resume cycle';
  document.getElementById('palette-now').textContent=caption;
  document.getElementById('palette-position').textContent=(paletteMode==='cycle'?'Cycling':'Held')+' · '+Math.round(percent)+'%';
  paletteButtons.forEach((button,i)=>button.setAttribute('aria-pressed',String(i===(blend>.5?(index+1)%paletteDefinitions.length:index))));
  for(const key of ['field','inner','halo','edge']){
    const rgb=lightRGB(currentColors[key],currentColors[key]).map(Math.round);
    const hex='#'+rgb.map(v=>v.toString(16).padStart(2,'0')).join('').toUpperCase();
    document.getElementById('palette-'+key+'-hex').textContent=hex;
    document.getElementById('palette-'+key+'-chip').style.backgroundColor=hex;
  }
  document.getElementById('halo-advice').textContent=haloStyle==='bridge'?'A lighter, softer blend of the field and center connects the two colors.':haloStyle==='contrast'?'A pale hue opposite the center gives a stronger separation.':a.note||'A lighter, restrained halo separates the center from the field without overpowering either.';
  document.getElementById('edge-value').textContent=Math.round(edgeLight*100)+'%';
}


// Fixed, almost invisible dither softens 8-bit bands without moving grain.
let lightDither = null;
if (typeof OffscreenCanvas !== 'undefined') {
  const tile = new OffscreenCanvas(128, 128);
  const tileContext = tile.getContext('2d');
  const pixels = tileContext.createImageData(128, 128);
  let seed = 173;
  for (let i = 0; i < pixels.data.length; i += 4) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    pixels.data[i] = pixels.data[i + 1] = pixels.data[i + 2] = seed >>> 24;
    pixels.data[i + 3] = 255;
  }
  tileContext.putImageData(pixels, 0, 0);
  lightDither = context.createPattern(tile, 'repeat');
}

// Themes receive source envelopes; future themes can use the same audio feed.
const themes = {
  'turell 1': (dt) => {
    // Fill the longer screen dimension, cropping the circle at the other edges.
    const radius = Math.max(width, height) / 2 * circleSize;
    const x = width / 2, y = height / 2;
    const motion = reducedMotion.matches ? 0.18 : 1;
    const response=responseProfile();
    const fieldLab=currentColors.field,discColor=currentColors.inner;
    const shifted=(lab,amount)=>[clamp(lab[0]+amount),lab[1],lab[2]];
    const color = index => lightColor(index<3?shifted(fieldLab,[.10,0,-.12][index]):shifted(discColor,index===3?-.015:.025));
    const halo=lightRGB(currentColors.halo,currentColors.halo).map(Math.round);

    context.fillStyle = '#000';
    context.fillRect(0, 0, width, height);
    context.save();
    context.beginPath();
    context.arc(x, y, radius, 0, Math.PI * 2);
    context.clip();

    // The paired light field stays perfectly circular, including in fullscreen.
    const field = context.createRadialGradient(x, y + radius * 0.34, 0, x, y, radius * 1.45);
    field.addColorStop(0, color(0));
    field.addColorStop(0.48, color(1));
    field.addColorStop(1, color(2));
    context.fillStyle = field;
    context.fillRect(x - radius, y - radius, radius * 2, radius * 2);

    // A second, inward-falling light wash follows the outer boundary.
    // Clipping preserves pure black beyond the circle even at full intensity.
    const edge=lightRGB(currentColors.edge,currentColors.edge).map(Math.round);
    const rimLevel=responseMode==='rim'||responseMode==='combined'?edgeLight*(.32+.95*response.rim):edgeLight;
    const rim=context.createRadialGradient(x,y,radius*(.70-.09*response.rim),x,y,radius);
    rim.addColorStop(0,`rgba(${edge},0)`);
    rim.addColorStop(.50,`rgba(${edge},${clamp(rimLevel*.08)})`);
    rim.addColorStop(.84,`rgba(${edge},${clamp(rimLevel*.38)})`);
    rim.addColorStop(1,`rgba(${edge},${clamp(rimLevel*.76)})`);
    context.fillStyle=rim;context.fillRect(x-radius,y-radius,radius*2,radius*2);

    const inner = radius * 0.28;
    const original=responseMode==='original';
    const glowRadius = inner * (2.65 + (original?(haloEnergy * .48 + haloPeak * .16)*motion:response.bloom*1.25));

    // Broad atmospheric light behind a tighter corona suggests depth while
    // keeping the composition as two simple, fixed-size circles.
    const aura = context.createRadialGradient(x, y, inner, x, y, glowRadius * 1.55);
    const auraAlpha = .1 + (original?haloEnergy*.06*motion:response.bloom*.14);
    aura.addColorStop(0, `rgba(${halo},${auraAlpha})`);
    aura.addColorStop(0.35, `rgba(${halo},${auraAlpha * 0.48})`);
    aura.addColorStop(1, `rgba(${halo},0)`);
    context.fillStyle = aura;
    context.fillRect(x - radius, y - radius, radius * 2, radius * 2);

    const glow = context.createRadialGradient(x, y, inner * 0.9, x, y, glowRadius);
    const strength = original?haloEnergy*motion*.06+haloPeak*motion*.04:response.bloom*.24;
    const glowStops = [
      [0,.90],[.09,.84],[.2,.64],[.34,.4],[.5,.2],[.7,.06],[.87,.012],[1,0],
    ];
    for (const [stop, alpha] of glowStops) {
      glow.addColorStop(stop, `rgba(${halo},${clamp(alpha * (1 + strength))})`);
    }
    context.fillStyle = glow;
    context.fillRect(x - glowRadius, y - glowRadius, glowRadius * 2, glowRadius * 2);

    if(response.shimmer>0){
      const shimmer=context.createRadialGradient(x,y,inner*.98,x,y,inner*1.26);
      const highlight=lightRGB(shifted(currentColors.halo,.15),shifted(currentColors.halo,.15)).map(Math.round);
      shimmer.addColorStop(0,`rgba(${highlight},${clamp(response.shimmer*.52)})`);
      shimmer.addColorStop(.32,`rgba(${highlight},${clamp(response.shimmer*.24)})`);
      shimmer.addColorStop(1,`rgba(${highlight},0)`);
      context.fillStyle=shimmer;context.fillRect(x-inner*1.3,y-inner*1.3,inner*2.6,inner*2.6);
    }

    if (lightDither) {
      context.save();
      context.globalCompositeOperation = 'soft-light';
      context.globalAlpha = 0.014;
      context.fillStyle = lightDither;
      context.fillRect(x - radius, y - radius, radius * 2, radius * 2);
      context.restore();
    }

    const disc = context.createRadialGradient(x-inner*.06,y-inner*.08,0,x,y,inner);
    const core=shifted(currentColors.core,response.depth*.10),discRim=shifted(currentColors.inner,-response.depth*.065);
    disc.addColorStop(0, lightColor(core));
    disc.addColorStop(.38, lightColor(core,discRim,.28));
    disc.addColorStop(.80, lightColor(core,discRim,.83));
    disc.addColorStop(1, lightColor(discRim));
    context.fillStyle = disc;
    context.beginPath();
    context.arc(x, y, inner, 0, Math.PI * 2);
    context.fill();
    context.restore();
  },
};

// A restrained 2D version remains available on devices without WebGL.
function drawSimpleTheme() {
  context.fillStyle='#000';context.fillRect(0,0,width,height);
  context.save();context.translate(width/2,height/2);
  const circular=!['well','overlap'].includes(activeTheme);
  const unit=(circular?Math.max(width,height)*circleSize:Math.min(width,height))*.36;context.scale(unit,unit);
  if(activeTheme==='well'){
    for(let i=0;i<5;i++){const size=1.12-i*.2;context.fillStyle=['#bfc4a6','#a9b095','#929b82','#7b856f','#080e19'][i];context.fillRect(-size,-size,size*2,size*2);}
  }else if(activeTheme==='turell 4'){
    const corona=context.createRadialGradient(0,0,.50,0,0,1.05);corona.addColorStop(0,'#fff2d6');corona.addColorStop(.08,'#d4c6ac');corona.addColorStop(.4,'#272725');corona.addColorStop(1,'#000');context.fillStyle=corona;context.beginPath();context.arc(0,0,1.05,0,Math.PI*2);context.fill();context.fillStyle='#000';context.beginPath();context.arc(0,0,.5,0,Math.PI*2);context.fill();
  }else if(['turell 2','turell 3','turell 5','turell lunar'].includes(activeTheme)){
    context.fillStyle=lightColor(currentColors.field);context.beginPath();context.arc(0,0,1.30,0,Math.PI*2);context.fill();context.fillStyle=activeTheme==='turell 2'?'#9cbbd1':activeTheme==='turell 5'?'#ee9b32':'#101b35';context.beginPath();context.arc(0,0,.36,0,Math.PI*2);context.fill();
  }else if(activeTheme==='water'){
    const waterLight=context.createRadialGradient(-.35,.5,.05,0,0,1.39);
    waterLight.addColorStop(0,'#59bdc4');waterLight.addColorStop(.6,'#1b6f86');waterLight.addColorStop(1,'#08374d');
    context.fillStyle=waterLight;context.beginPath();context.arc(0,0,1.39,0,Math.PI*2);context.fill();
  }else{
    context.globalCompositeOperation='screen';
    [[0,-.32,'#779fb5'],[-.33,.22,'#c39a7a'],[.33,.22,'#9992b9']].forEach(([x,y,color])=>{
      context.fillStyle=color;context.beginPath();context.arc(x,y,.62,0,Math.PI*2);context.fill();
    });
  }
  context.restore();
}

function setTheme(name, remember=true) {
  if(!Object.hasOwn(themeDetails,name))return;
  activeTheme=name;
  const off=name==='off';
  document.body.classList.toggle('visuals-off',off);
  document.getElementById('visualizer-off-message').hidden=!off;
  document.getElementById('enter-fullscreen').disabled=off;
  syncCircleControls();
  if(!off&&name!=='turell 1'&&!rendererAttempted){
    rendererAttempted=true;
    try {sceneRenderer=OctetThemes.create(sceneCanvas,message=>{notice.textContent=message;},name,
      {maxPixels:document.body.classList.contains('projection')?2073600:16777216});}catch{sceneRenderer=null;}
    sceneRenderer?.resize(width,height);
  }
  const [title,description]=themeDetails[name];
  document.getElementById('theme-title').textContent=title;
  document.getElementById('theme-description').textContent=description;
  document.getElementById('theme-select').value=name;
  document.getElementById('immersive-theme-select').value=name;
  stage.setAttribute('aria-label',title+' audiovisual theme');
  sceneCanvas.setAttribute('aria-label',description);
  canvas.setAttribute('aria-label',name==='turell 1'?'A fixed colored circle surrounded by a sound-reactive halo inside a large circular light field.':description);
  canvas.hidden=off||(name!=='turell 1'&&Boolean(sceneRenderer));
  sceneCanvas.hidden=off||name==='turell 1'||!sceneRenderer;
  const pairedColors=['turell 1','turell 2','turell 3','turell 5','turell lunar'].includes(name);
  for(const id of ['palette-control','immersive-palette-control','palette-caption','palette-panel','immersive-color-tools'])document.getElementById(id).hidden=!pairedColors;
  document.getElementById('visualizer-workbench').classList.toggle('colors-hidden',!pairedColors);
  document.getElementById('palette-media-note').hidden=name==='turell 1';
  document.getElementById('palette-media-note').textContent=name==='turell 5'?'Real Sun footage, modeled eclipse and a slowly moving corona. Field and halo follow the color cycle.':'Video stays natural. Field, halo and edge follow the cycle.';
  document.getElementById('solar-credit').hidden=name!=='turell 5';
  document.getElementById('lunar-credit').hidden=name!=='turell lunar';
  for(const id of ['response-panel','immersive-response-controls'])document.getElementById(id).hidden=name!=='turell 1';
  sceneRenderer?.setActive(name,off||document.hidden,reducedMotion.matches);
  sceneRenderer?.setSolarOptions(solarOptions);
  sceneRenderer?.setMediaZoom(videoZooms[name]||1);
  syncVideoControls();
  notice.textContent=!off&&name!=='turell 1'&&!sceneRenderer?'This device is showing a simplified version of the artwork.':'';
  if(remember){try{localStorage.setItem('octet.visualizer.theme',name);}catch{/* Rendering works without storage. */}}
  if(off){
    localSimulation.pause();setSimulationMode(false);
    feed?.close();feed=null;
    document.getElementById('hdmi-preview').hidden=true;hdmiPreviewActive=false;
    statusLabel.textContent='Visuals off';
    document.getElementById('render-fps').textContent='Off';
  }
  else if(name==='turell 1')themes[name](0);
  else if(sceneRenderer)sceneRenderer.render(name,artworkTime,haloEnergy,haloPeak,spectrum,shaderPalette);
  else drawSimpleTheme();
  resumeVisuals();
}

function resumeVisuals(){
  cancelAnimationFrame(animation);previousTime=0;
  if(activeTheme!=='off'&&!document.hidden){connect();animation=requestAnimationFrame(draw);}
}
document.getElementById('theme-select').onchange=event=>setTheme(event.target.value);
document.getElementById('immersive-theme-select').onchange=event=>{setTheme(event.target.value);showControls();};

const scrubThemes=['turell 2','turell 3','turell 5','turell lunar'];
let videoZooms={};
const solarOptions={tone:'natural',mode:'realism',speed:1};
try{
  const saved=JSON.parse(localStorage.getItem('octet.visualizer.solar')||'{}');
  if(['natural','cycle','yellow','amber','red'].includes(saved?.tone))solarOptions.tone=saved.tone;
  if(['realism','artistic'].includes(saved?.mode))solarOptions.mode=saved.mode;
  if([.25,.5,1,2,4].includes(saved?.speed))solarOptions.speed=saved.speed;
}catch{/* Optional eclipse preferences. */}
try{
  const saved=JSON.parse(localStorage.getItem('octet.visualizer.video-zoom')||'{}');
  for(const name of scrubThemes)if(Number.isFinite(saved?.[name]))videoZooms[name]=clamp(saved[name],.5,3);
}catch{/* Optional framing preferences. */}
function videoTime(seconds){
  if(!Number.isFinite(seconds)||seconds<0)return '0:00';
  const value=Math.floor(seconds);return Math.floor(value/60)+':'+String(value%60).padStart(2,'0');
}
let lastVideoUi=-Infinity;
function syncVideoControls(){
  const state=sceneRenderer?.mediaState(),visible=scrubThemes.includes(activeTheme)&&Boolean(state);
  for(const id of ['video-controls','immersive-video-controls'])document.getElementById(id).hidden=!visible;
  if(!visible)return;
  const solar=state.kind==='eclipse';
  document.getElementById('video-controls-title').textContent=solar?'Hybrid eclipse':'Background video';
  const ready=Number.isFinite(state.duration)&&state.duration>0;
  const text=ready?videoTime(state.time)+' / '+videoTime(state.duration)+(solar?' · '+state.phase:state.seeking?' · Seeking…':''):'Loading video…';
  for(const prefix of ['','immersive-']){
    const slider=document.getElementById(prefix+'video-scrub');
    slider.disabled=!ready;slider.max=String(ready?state.duration:1);slider.value=String(state.time||0);
    slider.setAttribute('aria-valuetext',text);
    slider.setAttribute('aria-label',(prefix?'Fullscreen ':'')+(solar?'Eclipse position':'Video position'));
    document.getElementById(prefix+'video-position-label').textContent=solar?'Eclipse position':'Video position';
    document.getElementById(prefix+'video-time').textContent=text;
    document.getElementById(prefix+'video-zoom').value=String(Math.round(state.zoom*100));
    document.getElementById(prefix+'video-zoom-value').textContent=Math.round(state.zoom*100)+'%';
    document.getElementById(prefix+'video-zoom-label').textContent=solar?'Sun zoom':'Video zoom';
    const play=document.getElementById(prefix+'video-play');
    play.textContent=state.paused?(solar?'Play eclipse':'Play video'):(solar?'Pause eclipse':'Pause video');play.disabled=reducedMotion.matches;
    document.getElementById(prefix+'solar-settings').hidden=!solar;
    if(solar){
      document.getElementById(prefix+'solar-treatment').value=state.tone;
      document.getElementById(prefix+'solar-mode').value=state.mode;
      document.getElementById(prefix+'solar-speed').value=String(state.speed);
    }
  }
  document.getElementById('video-scrub-note').textContent=reducedMotion.matches?'Motion is held for your reduced-motion preference. Scrub to preview still frames.':solar?'12-minute installation cycle · 4+ minutes of totality at 1×. Realism dims the field as a bright, slowly moving corona emerges.':'Scrub through this local video excerpt. Music keeps playing independently.';
}
for(const prefix of ['','immersive-']){
  document.getElementById(prefix+'video-scrub').oninput=event=>{
    sceneRenderer?.seekMedia(Number(event.target.value));syncVideoControls();showControls();
  };
  document.getElementById(prefix+'video-play').onclick=()=>{sceneRenderer?.toggleMedia();syncVideoControls();showControls();};
  document.getElementById(prefix+'video-zoom').oninput=event=>{
    const value=Number(event.target.value);if(!Number.isFinite(value))return;
    videoZooms[activeTheme]=clamp(value/100,.5,3);sceneRenderer?.setMediaZoom(videoZooms[activeTheme]);
    try{localStorage.setItem('octet.visualizer.video-zoom',JSON.stringify(videoZooms));}catch{/* Optional preference. */}
    syncVideoControls();showControls();
  };
  for(const [id,key] of [['solar-treatment','tone'],['solar-mode','mode'],['solar-speed','speed']])document.getElementById(prefix+id).onchange=event=>{
    const value=key==='speed'?Number(event.target.value):event.target.value;
    const allowed=key==='speed'?[.25,.5,1,2,4]:key==='tone'?['natural','cycle','yellow','amber','red']:['realism','artistic'];
    if(!allowed.includes(value))return;
    solarOptions[key]=value;sceneRenderer?.setSolarOptions(solarOptions);
    try{localStorage.setItem('octet.visualizer.solar',JSON.stringify(solarOptions));}catch{/* Optional preference. */}
    syncVideoControls();showControls();
  };
  for(const [id,key] of [['solar-sun','sun'],['solar-diamond','diamond'],['solar-totality-jump','totality']])document.getElementById(prefix+id).onclick=()=>{sceneRenderer?.seekMedia(OctetSolar.landmarks[key]);syncVideoControls();showControls();};
}

function resize() {
  const rect = stage.getBoundingClientRect();
  width = rect.width; height = rect.height;
  // Match the projection renderer: full 4K, bounded at 16 megapixels.
  const ratio = Math.min(devicePixelRatio || 1, 2, Math.sqrt(16777216 / Math.max(1, width * height)), 8192 / Math.max(1, width), 8192 / Math.max(1, height));
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  context.setTransform(canvas.width / Math.max(1, width), 0, 0, canvas.height / Math.max(1, height), 0, 0);
  sceneRenderer?.resize(width,height);
}
new ResizeObserver(resize).observe(stage);

let perfStart=0,perfLast=0,perfIntervals=[],perfFrames=0,perfVideoFrames={};
function measurePerformance(now){
  if(hdmiPreviewActive){
    document.getElementById('render-fps').textContent='Live HDMI preview';
    document.getElementById('render-performance').textContent='The artwork image is received from the Pi. Render and video cadence are measured on the HDMI renderer.';
    perfStart=now;perfLast=now;perfIntervals=[];perfFrames=0;return;
  }
  if(!perfStart){perfStart=now;perfLast=now;return;}
  const interval=now-perfLast;perfLast=now;
  if(interval>500){perfStart=now;perfIntervals=[];perfFrames=0;perfVideoFrames={};return;}
  perfIntervals.push(interval);perfFrames++;
  const elapsed=now-perfStart;if(elapsed<2000)return;
  const fps=perfFrames*1000/elapsed,ordered=perfIntervals.slice().sort((a,b)=>a-b);
  const p95=ordered[Math.min(ordered.length-1,Math.floor(ordered.length*.95))]||0;
  const state=activeTheme==='turell 1'?null:sceneRenderer?.performanceState();
  const videoLabels=(state?.videos||[]).map(video=>{
    const prior=perfVideoFrames[video.name];perfVideoFrames[video.name]=video.presented;
    const presented=prior===undefined?'measuring':((video.presented-prior)*1000/elapsed).toFixed(0)+' fps presented';
    return video.name+': '+(video.paused?'paused':presented)+' / '+video.sourceFps+' fps source'+(video.dropped===null?'':' · '+video.dropped+' dropped of '+video.total);
  });
  document.getElementById('render-fps').textContent=fps.toFixed(0)+' fps render'+(videoLabels.length?' · '+videoLabels.join(' · '):'');
  document.getElementById('render-performance').textContent='95% of frame intervals ≤ '+p95.toFixed(1)+' ms · '+(state?state.width+' × '+state.height+' · '+state.renderer:canvas.width+' × '+canvas.height+' · Canvas 2D');
  perfStart=now;perfIntervals=[];perfFrames=0;
}

function draw(now) {
  if(activeTheme==='off')return;
  measurePerformance(now);
  const elapsed = Math.max(0, (now - (previousTime || now)) / 1000);
  const dt = Math.min(elapsed, 0.1);
  previousTime = now;
  sceneRenderer?.advance(elapsed);
  if (simulating) {
    const sample=localSimulation.sample();
    targetEnergy=sample.energy;targetPeak=sample.peak;
    analysisBands=sample.bands;
    for(let i=0;i<4;i++)spectrum[i]+=(sample.bands[i]-spectrum[i])*(1-Math.exp(-dt/(i===3?2.8:.6)));
    ['sample-bass','sample-mid','sample-high'].forEach((id,i)=>{document.getElementById(id).value=spectrum[i];});
  } else if (now - lastSignal > 650) {
    targetEnergy = targetPeak = 0;
    if (sourceActive) {
      sourceActive = false;
      statusLabel.textContent = 'Audio disconnected · reconnecting…';
    }
  }
  // Read unsmoothed analysis here: the slow artwork envelopes would erase beats.
  const drives={bass:1-Math.exp(-(simulating?analysisBands[0]:targetEnergy)*4.5),
    body:1-Math.exp(-targetEnergy*2.8),rim:1-Math.exp(-targetEnergy*3),
    treble:1-Math.exp(-(simulating?analysisBands[2]+analysisBands[1]*.15:Math.max(0,targetPeak-targetEnergy))*7)};
  for(const key of Object.keys(responseEnvelope)){
    const value=responseEnvelope[key],drive=clamp(drives[key]);
    const tau=drive>value?(key==='rim'?.32:.035):(key==='rim'?1.2:key==='treble'?.22:.48);
    responseEnvelope[key]+=(drive-value)*(1-Math.exp(-dt/tau));
  }
  // Two stages soften changes in velocity as well as value, giving the light
  // gentle inertia instead of snapping the gradient on every transient.
  energy += (targetEnergy - energy) * (1 - Math.exp(-dt / (targetEnergy > energy ? 0.16 : 0.85)));
  peak += (targetPeak - peak) * (1 - Math.exp(-dt / (targetPeak > peak ? 0.10 : 0.55)));
  haloEnergy += (energy - haloEnergy) * (1 - Math.exp(-dt / 0.24));
  haloPeak += (peak - haloPeak) * (1 - Math.exp(-dt / 0.16));
  if(sharedTimeline){
    const shared=sharedTimeline();
    if(paletteMode==='cycle'&&!reducedMotion.matches)paletteClock=shared.paletteClock;
    if(!reducedMotion.matches)artworkTime=shared.artworkTime;
    updatePalette(0);
  }else{
    updatePalette(elapsed);
    artworkTime += elapsed * (reducedMotion.matches ? 0 : .7 + haloEnergy * .3);
  }
  if(!hdmiPreviewActive||simulating){
    if(activeTheme==='turell 1')themes[activeTheme](dt);
    else if(sceneRenderer)sceneRenderer.render(activeTheme,artworkTime,haloEnergy*(reducedMotion.matches?.18:1),haloPeak*(reducedMotion.matches?.18:1),simulating?spectrum:[haloEnergy,haloEnergy,haloPeak,.3],shaderPalette);
    else drawSimpleTheme();
  }
  projectionFrameSink?.(activeTheme==='turell 1'||!sceneRenderer?canvas:sceneCanvas);
  if(now-lastVideoUi>100){syncVideoControls();lastVideoUi=now;}
  animation = requestAnimationFrame(draw);
}

function envelope(values) {
  if (!Array.isArray(values)) return 0;
  const db = Math.max(-120, ...values.filter(value => typeof value === 'number' && Number.isFinite(value)));
  // A soft gate keeps silence still; musical dynamics retain plenty of headroom.
  return db <= -65 ? 0 : Math.pow(clamp((db + 65) / 60), 1.65);
}

function connect() {
  if (activeTheme==='off' || feed || document.hidden || simulating) return;
  feed = new EventSource('/api/visualizer');
  feed.onmessage = event => {
    if (simulating) return;
    try {
      const frame = JSON.parse(event.data);
      lastSignal = performance.now();
      sourceActive = Boolean(frame.available && frame.active);
      targetEnergy = sourceActive ? envelope(frame.rms) : 0;
      targetPeak = sourceActive ? envelope(frame.peak) : 0;
      const label = !frame.available ? 'Audio unavailable · reconnecting…'
        : sourceActive && targetPeak > 0 ? '● Reacting to source audio' : 'Waiting for audio';
      if (statusLabel.textContent !== label) statusLabel.textContent = label;
    } catch {
      targetEnergy = targetPeak = 0;
    }
  };
  feed.onerror = () => {
    if (simulating) return;
    targetEnergy = targetPeak = 0;
    sourceActive = false;
    statusLabel.textContent = 'Audio disconnected · reconnecting…';
  };
}

function setSimulationMode(enabled) {
  if(simulating===enabled)return;
  simulating = enabled;
  targetEnergy = targetPeak = 0;
  sourceActive = false;
  lastSignal = -Infinity;
  for (const id of ['simulate-music', 'immersive-simulate']) {
    const button = document.getElementById(id);
    button.textContent = simulating ? 'Stop simulation' : 'Simulate music';
    button.setAttribute('aria-pressed', String(simulating));
  }
  if (simulating) {
    feed?.close(); feed = null;
    statusLabel.textContent = 'Loading sample music…';
  } else {
    statusLabel.textContent = 'Connecting to audio…';
    connect();
  }
  document.getElementById('visualizer-source').textContent=simulating?'Simulated music · Octet':'Live source audio · Octet';
  document.getElementById('immersive-pause').hidden=!simulating;
  document.getElementById('sample-response').hidden=!simulating;
  syncResponseControls();syncPreviewButton();
  if (immersive()) showControls();
}
function sampleState(state) {
  if(state==='playing')setSimulationMode(true);
  syncPreviewButton();
  if(!simulating)return;
  const title=OctetSimulation.tracks[localSimulation.track()].title;
  statusLabel.textContent=state==='playing'?'● '+title:state==='loading'?'Buffering sample music…':'Sample music paused';
  document.getElementById('immersive-pause').textContent=sampleAudio.paused?'Play music':'Pause music';
}
function syncPreviewButton(){
  document.getElementById('response-preview').textContent=simulating&&!sampleAudio.paused?'Pause preview music':'Play preview music';
}
document.getElementById('response-preview').onclick=async()=>{
  notice.textContent='';
  if(simulating&&!sampleAudio.paused){localSimulation.pause();return;}
  setSimulationMode(true);
  try{await localSimulation.play();}catch(error){setSimulationMode(false);notice.textContent=error.message;}
};
async function toggleSimulation() {
  notice.textContent='';
  if(simulating){localSimulation.pause();setSimulationMode(false);return;}
  setSimulationMode(true);
  try{await localSimulation.play();}catch(error){setSimulationMode(false);notice.textContent=error.message;}
}
async function selectSample(name) {
  if(!Object.hasOwn(OctetSimulation.tracks,name))return;
  document.getElementById('simulation-track').value=name;
  document.getElementById('immersive-track').value=name;
  document.getElementById('sample-credit-title').textContent=OctetSimulation.tracks[name].title;
  try{localStorage.setItem('octet.visualizer.track',name);}catch{/* Optional preference. */}
  try{await localSimulation.select(name);}catch(error){notice.textContent=error.message;}
}
document.getElementById('simulation-track').onchange=event=>selectSample(event.target.value);
document.getElementById('immersive-track').onchange=event=>{selectSample(event.target.value);showControls();};
document.getElementById('immersive-pause').onclick=()=>{
  if(sampleAudio.paused)localSimulation.play().catch(error=>{notice.textContent=error.message;});
  else localSimulation.pause();
};
let savedTrack='edm';try{const value=localStorage.getItem('octet.visualizer.track');if(Object.hasOwn(OctetSimulation.tracks,value))savedTrack=value;}catch{/* Default track. */}
selectSample(savedTrack);
document.getElementById('simulate-music').onclick = toggleSimulation;
document.getElementById('immersive-simulate').onclick = toggleSimulation;

function immersive() {
  return document.fullscreenElement === stage || stage.classList.contains('immersive');
}
function showControls() {
  clearTimeout(controlsTimer);
  stage.classList.add('controls-visible');
  stage.classList.remove('controls-hidden');
  controlsTimer = setTimeout(() => {
    stage.classList.remove('controls-visible');
    if (immersive()) stage.classList.add('controls-hidden');
  }, 2400);
}
function syncFullscreen() {
  document.body.classList.toggle('immersed', immersive());
  if (immersive()) showControls();
  else {
    stage.classList.remove('controls-hidden');
    document.getElementById('enter-fullscreen').focus({preventScroll: true});
  }
}
async function enterFullscreen() {
  notice.textContent = '';
  try {
    if (!stage.requestFullscreen) throw Error('Fullscreen unavailable');
    await stage.requestFullscreen();
  } catch {
    // Also works in browsers that only support an immersive page view.
    stage.classList.add('immersive');
  }
  // Avoid keeping the exit button focused so the artwork can hide its controls.
  document.activeElement?.blur();
  syncFullscreen();
}
async function exitFullscreen() {
  try {
    if (document.fullscreenElement === stage) await document.exitFullscreen();
    stage.classList.remove('immersive');
    syncFullscreen();
  } catch {
    notice.textContent = 'Press Esc to exit full screen.';
  }
}
document.getElementById('enter-fullscreen').onclick = enterFullscreen;
document.getElementById('exit-fullscreen').onclick = exitFullscreen;
document.addEventListener('fullscreenchange', syncFullscreen);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && immersive()) exitFullscreen();
  else if (event.key === 'Tab' && immersive()) showControls();
});
stage.addEventListener('pointermove', showControls);
stage.addEventListener('pointerdown', showControls);
stage.addEventListener('pointerdown',()=>sceneRenderer?.resumeMedia());
reducedMotion.addEventListener?.('change',()=>sceneRenderer?.setActive(activeTheme,document.hidden,reducedMotion.matches));
document.addEventListener('visibilitychange', () => {
  sceneRenderer?.setActive(activeTheme,document.hidden,reducedMotion.matches);
  if (document.hidden) {
    feed?.close(); feed = null;
    cancelAnimationFrame(animation);
    targetEnergy = targetPeak = energy = peak = haloEnergy = haloPeak = 0;
    previousTime = 0;
    for(const key of Object.keys(responseEnvelope))responseEnvelope[key]=0;
  } else {
    resumeVisuals();
  }
});
window.addEventListener('pagehide', () => {rememberPalette();feed?.close();localSimulation.pause();sceneRenderer?.setActive(activeTheme,true,reducedMotion.matches);cancelAnimationFrame(animation);});
window.addEventListener('pageshow', event => {
  if (event.persisted) {feed = null;sceneRenderer?.setActive(activeTheme,document.hidden,reducedMotion.matches);resumeVisuals();}
});
resize();updatePalette(0);setTheme(activeTheme,false);
