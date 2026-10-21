/* Audio lifecycle and fullscreen behavior without Pi hardware. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'visualizer.js'), 'utf8');
const scenesSource = fs.readFileSync(path.join(__dirname, 'visualizer-themes.js'), 'utf8');
const elements = new Map(), listeners = new Map(), feeds = [], arcs = [], gradients = [];
const context2d = {
  fillRect(){}, save(){}, restore(){}, beginPath(){}, clip(){}, fill(){}, setTransform(){},
  translate(){},scale(){},moveTo(){},lineTo(){},closePath(){},
  arc(...args){arcs.push(args);},
  createRadialGradient(...args){gradients.push(args);return {addColorStop(){}};},
  createLinearGradient(){return {addColorStop(){}};},
};
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {textContent:'', getContext:type=>type==='webgl'?null:context2d,
      paused:true,children:[],append(...children){this.children.push(...children);},style:{setProperty(name,value){this[name]=value;}},
      getBoundingClientRect:()=>({width:1200,height:800}),
      classList:{add:name=>classes.add(name),remove:name=>classes.delete(name),contains:name=>classes.has(name),
        toggle(name,value){if(value)classes.add(name);else classes.delete(name);}},
      addEventListener(){},focus(){},blur(){},setAttribute(name,value){this[name]=value;},
    });
  }
  return elements.get(id);
}
let created=0;
const document = {hidden:false,fullscreenElement:null,activeElement:null,body:element('body'),
  createElement:()=>element('generated-'+created++),
  getElementById:element,addEventListener:(name,fn)=>listeners.set(name,fn)};
let now=1000, scheduled=0;
const storage=new Map();
let sampleCounter=0;
const sandbox = vm.createContext({document,console,Math,Number,Boolean,JSON,
  OctetSimulation:{tracks:{edm:{title:'EDM Detection Mode'},classical:{title:'Gymnopédie No. 1'}},create(audio,onState){return{
    play(){audio.paused=false;onState('playing');return Promise.resolve();},
    pause(){audio.paused=true;onState('paused');},select(){return Promise.resolve();},track:()=> 'edm',
    sample(){return audio.paused?{energy:0,peak:0,bands:[0,0,0,0]}:{energy:.7,peak:++sampleCounter%2?.8:.4,bands:[.8,.5,.3,.4]};},
  };}},
  localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
  matchMedia:()=>({matches:false}),devicePixelRatio:1,
  performance:{now:()=>now},
  ResizeObserver:class{observe(){}},
  EventSource:class{constructor(url){this.url=url;feeds.push(this);}close(){this.closed=true;}},
  requestAnimationFrame(){return ++scheduled;},cancelAnimationFrame(){},setTimeout(){},clearTimeout(){},
  window:{addEventListener:(name,fn)=>listeners.set(name,fn)},
});
const run = code=>vm.runInContext(code,sandbox);
run(fs.readFileSync(path.join(__dirname,'visualizer-solar.js'),'utf8'));
run(scenesSource);
run(source);
const frame = data=>feeds.at(-1).onmessage({data:JSON.stringify(data)});
(async()=>{
  assert.equal(feeds[0].url,'/api/visualizer');
  run("responseMode='original'");
  frame({available:true,active:true,rms:[-12,-14],peak:[-3,-4]});
  assert.ok(run('targetEnergy')>0.6);
  assert.match(element('signal-status').textContent,/Reacting/);
  run('draw(1100); draw(1200);');
  assert.equal(arcs[0][2],400); // Largest circle that fits 1200 × 800.
  const quietGlow = gradients.at(-2)[5];
  const fixedDisc = arcs.at(-1)[2];
  run('draw(1300);draw(1400);');
  assert.ok(gradients.at(-2)[5]>quietGlow);
  assert.equal(arcs.at(-1)[2],fixedDisc);


  // Silence and pause settle, offline and stale messages stop effects.
  frame({available:true,active:false,rms:[-1],peak:[0]});
  assert.equal(run('targetEnergy'),0);
  frame({available:true,active:true,rms:[-120],peak:[-120]});
  assert.equal(run('targetPeak'),0);
  frame({available:true,active:true,rms:[-10],peak:[-1]});
  run('draw(1800)');
  assert.equal(run('targetEnergy'),0);
  feeds[0].onerror();
  assert.equal(run('targetPeak'),0);
  feeds[0].onmessage({data:'malformed'});
  assert.equal(run('targetEnergy'),0);

  document.hidden=true;listeners.get('visibilitychange')();
  assert.equal(feeds[0].closed,true);
  document.hidden=false;listeners.get('visibilitychange')();
  assert.equal(feeds.length,2);

  // Unsupported Fullscreen API falls back to an immersive view; Escape exits it.
  await run('enterFullscreen()');
  assert.equal(run('immersive()'),true);
  listeners.get('keydown')({key:'Escape'});
  assert.equal(run('immersive()'),false);

  const stage=element('visualizer-stage');
  stage.requestFullscreen=async()=>{document.fullscreenElement=stage;};
  document.exitFullscreen=async()=>{document.fullscreenElement=null;};
  await run('enterFullscreen()');
  assert.equal(document.fullscreenElement,stage);
  await run('exitFullscreen()');
  assert.equal(document.fullscreenElement,null);

  // Simulation works offline, closes the live feed, and restores it on stop.
  element('simulate-music').onclick();
  assert.equal(run('simulating'),true);
  assert.equal(feeds.at(-1).closed,true);
  assert.equal(element('simulate-music')['aria-pressed'],'true');
  assert.match(element('signal-status').textContent,/EDM/);
  run('draw(2000)');
  assert.ok(run('targetEnergy')>0);
  const firstPulse=run('targetPeak');
  run('draw(2100)');
  assert.notEqual(run('targetPeak'),firstPulse);
  feeds.at(-1).onerror(); // A queued old event cannot overwrite simulation.
  assert.match(element('signal-status').textContent,/EDM/);
  element('immersive-simulate').onclick();
  assert.equal(run('simulating'),false);
  assert.equal(feeds.length,3);
  assert.equal(element('simulate-music')['aria-pressed'],'false');

  // All themes switch without changing audio connections; both selectors and
  // saved preference agree, including in browsers with no WebGL support.
  const feedCount=feeds.length;
  for(const name of ['turell 2','turell 3','turell 4','turell 5','turell lunar','well','overlap','water','turell 1']){
    element('theme-select').onchange({target:{value:name}});
    assert.equal(run('activeTheme'),name);
    assert.equal(element('immersive-theme-select').value,name);
    assert.equal(storage.get('octet.visualizer.theme'),name);
    run('draw(2200)');
  }
  element('immersive-theme-select').onchange({target:{value:'well'}});
  assert.equal(element('theme-select').value,'well');
  assert.match(element('theme-title').textContent,/Well/);
  assert.equal(feeds.length,feedCount);
  run("setTheme('../../invalid')");
  assert.equal(run('activeTheme'),'well');
  for(const retired of ['hinge','interval','balance']){
    run('setTheme('+JSON.stringify(retired)+')');
    assert.equal(run('activeTheme'),'well');
  }
  assert.equal(element('palette-select').children.length,12);
  assert.equal(element('palette-swatches').children.length,12);
  const initialInner=run('currentColors.inner.slice()');
  element('palette-select').onchange({target:{value:'watermelon'}});
  assert.equal(element('immersive-palette').value,'watermelon');
  assert.equal(storage.get('octet.visualizer.palette'),'watermelon');
  run('updatePalette(10)');
  assert.notDeepEqual(run('currentColors.inner.slice()'),initialInner);
  run("setTheme('turell 1');draw(2300)");
  assert.equal(arcs.at(-1)[2],fixedDisc);
  element('immersive-palette').onchange({target:{value:'cycle'}});
  assert.equal(element('palette-select').value,'cycle');
  run("setPalette('invalid')");assert.equal(run('paletteMode'),'cycle');
  // Scrubbing holds the exact blend; resume continues at that position.
  element('palette-scrub').oninput({target:{value:'1650'}});
  assert.equal(run('paletteMode'),'scrub');
  assert.equal(element('immersive-palette-scrub').value,'1650');
  const heldClock=run('paletteClock'),heldColors=run('JSON.stringify(currentColors)');
  run('updatePalette(10)');assert.equal(run('paletteClock'),heldClock);
  assert.equal(run('JSON.stringify(currentColors)'),heldColors);
  element('palette-play').onclick();assert.equal(run('paletteMode'),'cycle');
  assert.equal(run('paletteClock'),heldClock);
  run('updatePalette(1)');assert.ok(run('paletteClock')>heldClock);
  element('immersive-palette-scrub').oninput({target:{value:'12000'}});
  assert.equal(element('palette-scrub').value,'12000');
  assert.deepEqual(run('currentColors.inner'),run('paletteDefinitions[0].innerLab'));
  element('palette-swatches').children[2].onclick();
  assert.equal(run('paletteMode'),'ultraviolet');
  const fieldBefore=run('JSON.stringify(currentColors.field)'),haloBefore=run('JSON.stringify(currentColors.halo)');
  element('halo-style').onchange({target:{value:'bridge'}});
  assert.equal(run('JSON.stringify(currentColors.field)'),fieldBefore);
  assert.notEqual(run('JSON.stringify(currentColors.halo)'),haloBefore);
  element('edge-light').oninput({target:{value:'100'}});
  assert.equal(run('shaderPalette.edgeLight'),1);
  assert.equal(storage.get('octet.visualizer.edge'),'1');
  assert.match(element('palette-panel').style['--field-track'],/linear-gradient/);
  assert.equal(arcs.at(-1)[2],fixedDisc);
  // Each comparison isolates its light layer; Combined excludes shimmer.
  run('Object.assign(responseEnvelope,{bass:.8,body:.6,rim:.5,treble:.7})');
  for(const [mode,key] of [['bloom','bloom'],['depth','depth'],['rim','rim'],['shimmer','shimmer']]){
    const profile=run('responseProfile('+JSON.stringify(mode)+',"balanced")');
    assert.ok(profile[key]>0);
    assert.ok(Object.entries(profile).filter(([name])=>name!==key).every(([,value])=>value===0));
    element('response-mode').onchange({target:{value:mode}});
    assert.equal(element('immersive-response-mode').value,mode);
    assert.equal(storage.get('octet.visualizer.response'),mode);
    run("themes['turell 1'](0)");assert.equal(arcs.at(-1)[2],fixedDisc);
  }
  assert.ok(Object.values(run('responseProfile("original")')).every(value=>value===0));
  const combined=run('responseProfile("combined")');
  assert.ok(combined.bloom>0&&combined.depth>0&&combined.rim>0);assert.equal(combined.shimmer,0);
  element('immersive-response-intensity').onchange({target:{value:'strong'}});
  assert.equal(element('response-intensity').value,'strong');
  assert.equal(storage.get('octet.visualizer.intensity'),'strong');
  assert.ok(run('responseProfile("bloom","strong").bloom')>run('responseProfile("bloom","subtle").bloom'));
  await element('response-preview').onclick();assert.equal(element('simulation-audio').paused,false);
  assert.equal(element('response-preview').textContent,'Pause preview music');
  run('draw(2400)');assert.ok(run('responseEnvelope.bass')>.8); // Fast attack reads raw FFT.
  await element('response-preview').onclick();assert.equal(element('simulation-audio').paused,true);
  assert.equal(element('response-preview').textContent,'Play preview music');
  const responseBefore=run('responseEnvelope.bass');run('draw(2500)');assert.ok(run('responseEnvelope.bass')<responseBefore);
  console.log('Visualizer checks passed: fixed geometry, signal/audio lifecycle, fullscreen/Escape, nine themes, 12 palette previews, exact scrub/hold/resume, wrap, halo comparison, outer edge controls and persistence.');
})().catch(error=>{console.error(error);process.exitCode=1;});
