/* Source controls in a real browser, with no speaker playback. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const sources = [
  {id:'tidal',label:'Tidal',group:'Music',available:true,description:'Tidal and the saved music queue.'},
  {id:'usb:iD14:pair-1',label:'Audient iD14 · Inputs 1 / 2 · stereo',group:'Audient iD14',available:true,description:'Stereo inputs.'},
  {id:'usb:iD14:mono-1',label:'Audient iD14 · Input 1 · mono',group:'Audient iD14',available:true,description:'Mono input.'},
  {id:'usb:iD14:mono-2',label:'Audient iD14 · Input 2 · mono',group:'Audient iD14',available:true,description:'Mono input.'},
];
const initial = () => ({source:'tidal',source_label:'Tidal',sources,source_switching_available:true,
  ready:true,muted:false,volume:-31,min_db:-60,max_db:-18,level_unlocked:false,profile:'Living room tuning',profile_description:'Saved system tuning',
  playback:'pause',duration:90,elapsed:20,song_position:0,current:{title:'Saved song',artist:'Saved artist'},
  queue:[{position:0,title:'Saved song',artist:'Saved artist'}]});
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:1000}});
    const errors=[],writes=[];let state=initial(),reject=false;
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://octet.test/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/status')return route.fulfill({json:state});
      if(url.pathname==='/api/input-levels')return route.fulfill({json:{inputs:[],age_ms:0,message:'No USB audio inputs connected.'}});
      if(url.pathname==='/api/favorites')return route.fulfill({json:{tracks:[{id:'1',title:'Library song',artist:'Artist',album:'Album',duration:90}]}});
      if(url.pathname==='/api/control/source'){
        const body=route.request().postDataJSON();writes.push(body);
        if(reject)return route.fulfill({status:503,json:{error:'Source change failed; previous source restored'}});
        state={...state,source:body.id,source_label:sources.find(row=>row.id===body.id).label,muted:true};
        return route.fulfill({json:state});
      }
      const asset=url.pathname==='/'?'index.html':url.pathname.slice(1);
      return route.fulfill({body:fs.readFileSync(path.join(__dirname,asset)),contentType:asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://octet.test/');
    await page.waitForFunction(()=>document.getElementById('source-choice').options.length===4);
    assert.equal(await page.locator('#source-choice').inputValue(),'tidal');
    assert.equal(await page.locator('#source-choice').isEnabled(),true);
    assert.equal(await page.locator('#dsp-profile-name').textContent(),'Living room tuning');
    assert.equal(await page.locator('#source-apply').isDisabled(),true);
    await page.selectOption('#source-choice','usb:iD14:mono-1');
    // A status poll must not discard a source selection that has not been applied.
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#source-choice').inputValue(),'usb:iD14:mono-1');
    await page.click('#source-apply');
    await page.waitForFunction(()=>document.getElementById('player-title').textContent==='LIVE INPUT');
    assert.equal(await page.locator('.transport').isVisible(),false);
    assert.equal(await page.locator('#seek').isVisible(),false);
    assert.equal(await page.getByRole('button',{name:'Library song',exact:true}).isDisabled(),true);
    assert.equal(await page.locator('#mute').textContent(),'Unmute');
    assert.match(await page.locator('#source-status').textContent(),/muted/);
    assert.equal(await page.locator('#volume').getAttribute('aria-valuetext'),'-31 dB');
    assert.equal(await page.locator('#dsp-profile-name').textContent(),'Living room tuning');
    assert.equal(await page.locator('#queue-count').textContent(),'0'); // The current song is not upcoming.
    await page.screenshot({path:path.resolve(__dirname,'../../out/music-source-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.resolve(__dirname,'../../out/music-source-mobile.png'),fullPage:true});
    reject=true;await page.selectOption('#source-choice','tidal');await page.click('#source-apply');
    await page.waitForFunction(()=>document.getElementById('source-status').textContent.includes('Source change failed'));
    assert.equal(await page.locator('#source-choice').inputValue(),'usb:iD14:mono-1');
    reject=false;await page.selectOption('#source-choice','tidal');await page.click('#source-apply');
    await page.waitForFunction(()=>document.getElementById('player-title').textContent==='NOW PLAYING');
    assert.equal(await page.locator('.transport').isVisible(),true);
    assert.equal(await page.getByRole('button',{name:'Library song',exact:true}).isDisabled(),false);
    assert.equal(await page.locator('#song-title').textContent(),'Saved song');
    assert.equal(await page.locator('#dsp-profile-name').textContent(),'Living room tuning');
    state={...state,ready:false,source_switching_available:false,source_switching_reason:'The current speaker configuration is not approved for playback.'};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#source-choice').isDisabled(),true);
    assert.equal(await page.locator('#source-apply').isDisabled(),true);
    assert.equal(await page.locator('#source-status').textContent(),state.source_switching_reason);
    delete state.source_switching_reason;
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#source-status').textContent(),'Connect to the speaker DSP to change sources.');
    assert.doesNotMatch(await page.locator('#source-status').textContent(),/Output DSP controls/);
    assert.deepEqual(writes,[{id:'usb:iD14:mono-1'},{id:'tidal'},{id:'tidal'}]);
    assert.deepEqual(errors,[]);
    console.log('Browser source flow passed: saved tuning retained, selection survives polls, live-input controls, queue and level retention, error recovery, Tidal return, unavailable reasons, mobile layout.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
