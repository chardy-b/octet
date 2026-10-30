const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'visualizer-sync.js'),'utf8');
const settings={theme:'turell 2',palette:'cycle',paletteClock:32,halo:'curated',edge:.35,
  response:'combined',intensity:'balanced',circleSize:1,zoom:{},solar:{tone:'natural',mode:'realism',speed:1},
  media:{theme:'turell 2',time:10,paused:false}};
const envelope={settings,updated:100,serverTime:110,animationEpoch:50,revision:'a',previewRequested:true,display:{online:true,revision:'a'}};
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function client(projection,initialNow){
  let now=initialNow,state=structuredClone(envelope),deferredPost,frameResponse={status:503};
  const listeners={},timers=[],seeks=[],elements=new Map(),copies=[],uploads=[],revoked=[],posts=[];
  const copy={getContext:()=>({drawImage:(...args)=>copies.push(args)}),toBlob:fn=>fn({size:100})};
  const media={time:20,duration:90,paused:false,seeking:false};
  const element=id=>{if(!elements.has(id))elements.set(id,{value:'',append(){},setAttribute(){}});return elements.get(id);};
  const sandbox={performance:{now:()=>now},Math,Number,JSON,Boolean,innerWidth:1920,innerHeight:1080,
    activeTheme:'turell 2',paletteMode:'cycle',paletteClock:32,haloStyle:'curated',edgeLight:.35,
    responseMode:'combined',responseIntensity:'balanced',circleSize:1,solarOptions:{...settings.solar},videoZooms:{},
    sharedTimeline:null,projectionFrameSink:null,simulating:false,hdmiPreviewActive:false,stage:{getBoundingClientRect:()=>({x:0,y:0,width:1920,height:1080})},
    reducedMotion:{matches:false},
    document:{body:{classList:{contains:()=>projection}},createElement:tag=>tag==='canvas'?copy:element('indicator'),querySelector:()=>element('actions'),
      querySelectorAll:()=>[],getElementById:element,addEventListener:(type,fn)=>listeners[type]=fn},
    window:{addEventListener(){}},clearTimeout(){},setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},
    URL:{createObjectURL:()=> 'blob:frame',revokeObjectURL:url=>revoked.push(url)},
    fetch:async(_url,options)=>{
      if(_url.startsWith('/api/visualizer/frame'))return frameResponse;
      if(_url.startsWith('/api/control/visualizer-frame')){uploads.push({_url,options});return {ok:true};}
      if(options?.method==='POST')posts.push(JSON.parse(options.body));
      if(options?.method==='POST'&&deferredPost)return await deferredPost;
      return {ok:true,json:async()=>structuredClone(state)};
    },
    syncResponseControls(){},rememberPalette(){},refreshCycleTracks(){},updatePalette(){},syncVideoControls(){},
    sceneRenderer:{mediaState:()=>({...media}),seekMedia(time){seeks.push(time);media.time=time;},toggleMedia(){media.paused=!media.paused;},setSolarOptions(){},setMediaZoom(){}},
  };
  sandbox.setPalette=mode=>sandbox.paletteMode=mode;
  sandbox.setTheme=theme=>sandbox.activeTheme=theme;
  sandbox.setCircleSize=size=>sandbox.circleSize=size;
  vm.runInNewContext(source,sandbox);
  return {sandbox,media,seeks,timers,listeners,copies,uploads,posts,element,advance(ms){now+=ms;},server(value){state=structuredClone(value);},
    frame(value){frameResponse=value;},async pollFrame(){await timers.findLast(t=>t.ms===120).fn();await flush();},
    deferPost(){let resolve;deferredPost=new Promise(r=>resolve=r);return resolve;},
    async poll(){const timer=timers.findLast(t=>t.ms===1000);await timer.fn();await flush();},
    async edit(id='palette-select'){listeners.change({type:'change',target:{id}});await timers.findLast(t=>t.ms===120).fn();await flush();}};
}
(async()=>{
  const preview=client(false,500),hdmi=client(true,900000);
  await flush();
  assert.equal(preview.sandbox.paletteClock,39.5,'Initial elapsed time uses the actual cycle rate');
  assert.equal(hdmi.sandbox.paletteClock,39.5);
  // Different page origins and a long frame/render stall do not change phase.
  preview.advance(15000);hdmi.advance(15000);
  assert.equal(preview.sandbox.sharedTimeline().paletteClock,50.75);
  assert.equal(hdmi.sandbox.sharedTimeline().paletteClock,50.75);
  assert.equal(preview.sandbox.sharedTimeline().artworkTime,52.5);
  assert.equal(hdmi.sandbox.sharedTimeline().artworkTime,52.5);
  // A decoder that fell behind is corrected without a settings revision change.
  const initialSeeks=hdmi.seeks.length;
  hdmi.media.time=2;hdmi.server({...envelope,serverTime:125});await hdmi.poll();
  assert.equal(hdmi.seeks.length,initialSeeks,'The HDMI decoder runs freely between actual control changes');
  const controller=client(false,0);await flush();controller.media.time=2;
  controller.server({...envelope,serverTime:125});await controller.poll();
  assert.equal(controller.seeks.at(-1),35);
  controller.media.seeking=true;const seekCount=controller.seeks.length;
  await controller.poll();assert.equal(controller.seeks.length,seekCount,'Do not restart an unfinished video seek');
  controller.media.seeking=false;controller.media.time=1;
  await controller.poll();assert.equal(controller.seeks.length,seekCount,'Allow decoded playback to settle after a seek');
  controller.advance(6000);controller.server({...envelope,serverTime:131});
  await controller.poll();assert.equal(controller.seeks.at(-1),41);
  const sizing=client(false,0);await flush();sizing.sandbox.circleSize=.65;
  const resized={...envelope,settings:{...settings,circleSize:.65},revision:'resized'};
  sizing.server(resized);await sizing.edit('circle-size');
  assert.equal(sizing.posts.at(-1).circleSize,.65);
  const sizedDisplay=client(true,0);await flush();
  sizedDisplay.server(resized);await sizedDisplay.poll();assert.equal(sizedDisplay.sandbox.circleSize,.65);
  hdmi.media.seeking=true;hdmi.media.paused=false;
  hdmi.server({...envelope,settings:{...settings,media:{...settings.media,time:41,paused:true}},revision:'paused'});
  await hdmi.poll();assert.equal(hdmi.media.paused,true,'Pause applies even while a seek is finishing');
  // The controller also adopts the canonical response after saving an edit.
  const saved={...envelope,settings:{...settings,paletteClock:80},updated:125,serverTime:125,revision:'b'};
  preview.server(saved);await preview.edit();
  assert.equal(Object.hasOwn(preview.posts.at(-1),'theme'),false,'Palette edits cannot enable stale themes');
  assert.equal(preview.sandbox.sharedTimeline().paletteClock,80);
  // A newer local edit must survive an older in-flight save response.
  const resolve=preview.deferPost();
  const save=preview.edit();await flush();
  preview.listeners.input({target:{id:'palette-scrub'}});
  assert.equal(preview.sandbox.sharedTimeline,null);
  resolve({ok:true,json:async()=>saved});await save;
  assert.equal(preview.sandbox.sharedTimeline,null);
  // Frames preserve the complete projection framing, remain bounded, and
  // are only captured while a controller requests the live preview.
  const capture=client(true,0);await flush();
  const canvas={width:3840,height:2160};
  capture.sandbox.projectionFrameSink(canvas);await flush();
  assert.deepEqual(capture.copies[0],[canvas,0,0,3840,2160,0,0,480,270]);
  assert.equal(capture.uploads[0].options.headers['Content-Type'],'image/jpeg');
  assert.match(capture.uploads[0]._url,/revision=a$/);
  capture.advance(100);capture.sandbox.projectionFrameSink(canvas);assert.equal(capture.uploads.length,1);
  capture.server({...envelope,previewRequested:false});await capture.poll();
  capture.advance(1000);capture.sandbox.projectionFrameSink(canvas);assert.equal(capture.uploads.length,1);
  const viewer=client(false,0);await flush();
  viewer.frame({status:200,blob:async()=>({}),headers:{get:()=> '11'}});
  await viewer.pollFrame();viewer.element('hdmi-preview').onload();
  assert.equal(viewer.element('hdmi-preview').hidden,false);
  assert.equal(viewer.sandbox.hdmiPreviewActive,true);
  assert.equal(viewer.element('hdmi-preview').src,'blob:frame');
  viewer.sandbox.simulating=true;await viewer.pollFrame();
  assert.equal(viewer.element('hdmi-preview').hidden,true,'Local sample preview remains local');
  assert.equal(viewer.sandbox.hdmiPreviewActive,false);
  viewer.sandbox.simulating=false;viewer.advance(9000);viewer.frame({status:503});await viewer.pollFrame();
  assert.equal(viewer.element('hdmi-preview').hidden,true,'Stale frames cannot masquerade as live HDMI');
  const off=client(false,0);await flush();
  const disabled={...envelope,settings:{...settings,theme:'off'},revision:'off',display:{online:false,revision:''}};
  off.server(disabled);await off.poll();
  assert.equal(off.sandbox.activeTheme,'off');
  assert.equal(off.element('indicator').textContent,'Visuals off');
  assert.equal(off.element('hdmi-preview').hidden,true);
  off.sandbox.activeTheme='water';off.server({...envelope,settings:{...settings,theme:'water'},revision:'water'});
  await off.edit('theme-select');
  assert.equal(off.posts.at(-1).theme,'water','Explicit theme selection enables HDMI');
  off.sandbox.activeTheme='off';off.server(disabled);await off.edit('immersive-theme-select');
  assert.equal(off.posts.at(-1).theme,'off','Fullscreen selector also stops HDMI');
  console.log('Visualizer sync checks passed: Off controls, clocks, media alignment, seek protection, and save races.');
})().catch(error=>{console.error(error);process.exitCode=1;});
