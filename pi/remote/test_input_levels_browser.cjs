/* Live meter rendering and latching without physical audio or speaker output. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const input=(channel,peak,rms,clip=0)=>({id:`usb:iD14:mono-${channel}`,device:'Audient iD14',label:`Input ${channel}`,available:true,peak_db:peak,rms_db:rms,clip_seq:clip,status:peak>=-80?'Signal':'Quiet'});
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});const errors=[];let mode='normal',requests=0;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://octet.test/**',async route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/api/status')return route.fulfill({json:{source:'tidal',source_label:'Tidal',sources:[{id:'tidal',label:'Tidal',group:'Music',available:true}],source_switching_available:true,ready:true,muted:true,volume:-36,min_db:-60,max_db:-18,profile:'Output DSP controls',profile_description:'Eight outputs',playback:'stop',duration:0,elapsed:0,queue:[]}});
   if(url.pathname==='/api/favorites')return route.fulfill({json:{tracks:[]}});
   if(url.pathname==='/api/input-levels'){
    requests++;
    if(mode==='error')return route.fulfill({status:503,json:{error:'Disconnected'}});
    const inputs=mode==='normal'?[input(1,-21,-34),input(2,-99,-110)]:mode==='clip'?[input(1,0,-8,1),input(2,-2,-10)]:[input(1,-30,-45,1),input(2,-100,-110)];
    const outputs=Array.from({length:8},(_,i)=>({...input(i+1,-20-i,-40-i),id:'output:'+(i+1),device:['EX30 · Left','EX30 · Right','EX32 · Left','EX32 · Right','Subwoofer · Left','Subwoofer · Right','Tweeter · Left','Tweeter · Right'][i],label:'Output '+(i+1),clip_seq:i===2&&mode!=='normal'?1:0,peak_db:i===2&&mode==='clip'?0:-20-i}));
    return route.fulfill({json:{inputs,outputs,output_message:'Digital levels after speaker DSP.',age_ms:mode==='stale'?1500:0,message:'Levels before speaker volume and EQ · clipping stays lit until reset.'}});
   }
   const asset=url.pathname==='/'?'index.html':url.pathname.slice(1);
   return route.fulfill({body:fs.readFileSync(path.join(__dirname,asset)),contentType:asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':'text/html'});
  });
  await page.goto('http://octet.test/');
  await page.waitForFunction(()=>document.querySelectorAll('#input-meters .input-meter').length===2);
  assert.equal(await page.locator('#output-meters .output-meter').count(),8);
  assert.deepEqual(await page.locator('#output-meters h3').allTextContents(),Array.from({length:8},(_,i)=>'Output '+(i+1)));
  const output=page.locator('#output-meters .output-meter').nth(2);
  const first=page.locator('#input-meters .input-meter').nth(0),second=page.locator('#input-meters .input-meter').nth(1);
  assert.equal(await first.locator('.input-meter-badge').textContent(),'Signal');
  assert.equal(await second.locator('.input-meter-badge').textContent(),'Quiet');
  assert.match(await first.locator('.input-meter-readout').textContent(),/Peak −21.0 dBFS/);
  assert.equal(await page.locator('#reset-input-clips').isDisabled(),true);
  mode='clip';await page.waitForFunction(()=>document.querySelector('#input-meters .input-meter.clipped'));
  assert.equal(await first.locator('.input-meter-badge').textContent(),'CLIP');
  assert.equal(await second.locator('.input-meter-badge').textContent(),'Near clip');
  mode='quiet';await page.waitForFunction(()=>document.querySelector('#input-meters .input-meter-readout').textContent.includes('−30.0'));
  assert.equal(await first.locator('.input-meter-badge').textContent(),'CLIP');
  assert.equal(await output.locator('.input-meter-badge').textContent(),'CLIP');
  await page.screenshot({path:path.resolve(__dirname,'../../out/io-meters-desktop.png'),fullPage:true});
  await page.click('#reset-input-clips');
  assert.equal(await first.locator('.input-meter-badge').textContent(),'Signal');
  assert.equal(await output.locator('.input-meter-badge').textContent(),'CLIP');
  await page.click('#reset-output-clips');
  assert.equal(await output.locator('.input-meter-badge').textContent(),'Signal');
  const prior=requests;await page.waitForFunction(()=>document.querySelector('#input-meters .input-meter-badge').textContent==='Signal');
  assert.equal(await page.locator('#reset-input-clips').isDisabled(),true);
  mode='stale';await page.waitForFunction(()=>document.querySelectorAll('#input-meters .input-meter.unavailable').length===2);
  assert.equal(await first.locator('.input-meter-rms').evaluate(el=>el.style.width),'0%');
  assert.match(await first.locator('.input-meter-readout').textContent(),/unavailable/);
  mode='error';await page.waitForFunction(()=>document.querySelector('#input-meter-note').textContent.includes('reconnecting'));
  mode='normal';await page.waitForFunction(()=>document.querySelector('#input-meters .input-meter-badge').textContent==='Signal');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:path.resolve(__dirname,'../../out/io-meters-mobile.png'),fullPage:true});
  assert.equal(await page.getByRole('meter').count(),10);assert.deepEqual(errors,[]);assert.ok(requests>prior);
  console.log('Input meter browser checks passed: both inputs during Tidal, RMS/peak, clipping latch/reset, near clipping, stale/error clearing, reconnect and mobile layout; eight ordered outputs with independent clip reset.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
