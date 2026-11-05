/* Source controls in a real browser, with no speaker playback. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const sources = [
  {id:'airplay',label:'AirPlay',group:'Music',available:true,description:'Cast to Octet on your LAN.'},
  {id:'tidal',label:'Tidal',group:'Music',available:true,description:'Tidal and the saved music queue.'},
  {id:'usb:iD14:pair-1',label:'Audient iD14 · Inputs 1 / 2 · stereo',group:'Audient iD14',available:true,description:'Stereo inputs.'},
  {id:'usb:iD14:mono-1',label:'Audient iD14 · Input 1 · mono',group:'Audient iD14',available:true,description:'Mono input.'},
  {id:'usb:iD14:mono-2',label:'Audient iD14 · Input 2 · mono',group:'Audient iD14',available:true,description:'Mono input.'},
];
const initial = () => ({source:'tidal',source_label:'Tidal',sources,source_switching_available:true,
  ready:true,muted:false,volume:-31,min_db:-60,max_db:-18,level_unlocked:false,profile:'Living room tuning',profile_description:'Saved system tuning',
  playback:'pause',duration:90,elapsed:20,song_position:0,current:{title:'Saved song',artist:'Saved artist',album:'Saved album'},
  queue:[{position:0,title:'Saved song',artist:'Saved artist'}]});
(async()=>{
  const browser=await chromium.launch(process.env.OCTET_BROWSER_EXECUTABLE
    ? {headless:true,executablePath:process.env.OCTET_BROWSER_EXECUTABLE,args:['--no-sandbox']}
    : {headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:1000}});
    const errors=[],writes=[];let state=initial(),reject=false,nowPlaying=null;
    let releaseSwitch, releasePoll, pollStarted, holdNextPoll=false;
    const switchGate=new Promise(resolve=>{releaseSwitch=resolve;});
    const pollGate=new Promise(resolve=>{releasePoll=resolve;});
    const oldPollStarted=new Promise(resolve=>{pollStarted=resolve;});
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://octet.test/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/status'){
        const snapshot=state;
        if(holdNextPoll){holdNextPoll=false;pollStarted();await pollGate;}
        return route.fulfill({json:snapshot});
      }
      if(url.pathname==='/api/now-playing')return route.fulfill({json:{song:nowPlaying}});
      if(url.pathname.startsWith('/api/now-playing/artwork/'))return route.fulfill({body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/Scv2WQAAAABJRU5ErkJggg==','base64'),contentType:'image/png'});
      if(url.pathname==='/api/input-levels')return route.fulfill({json:{inputs:[],age_ms:0,message:'No USB audio inputs connected.'}});
      if(url.pathname==='/api/favorites')return route.fulfill({json:{tracks:[{id:'1',title:'Library song',artist:'Artist',album:'Album',duration:90}]}});
      if(url.pathname==='/api/control/mute')return route.fulfill({status:409,json:{error:'The speaker DSP is waiting for input audio.'}});
      if(url.pathname==='/api/control/source'){
        const body=route.request().postDataJSON();writes.push(body);
        if(writes.length===1)await switchGate;
        if(reject)return route.fulfill({status:503,json:{error:'Source change failed; previous source restored'}});
        state={...state,source:body.id,source_label:sources.find(row=>row.id===body.id).label,muted:true};
        return route.fulfill({json:state});
      }
      const asset=url.pathname==='/'?'index.html':url.pathname.slice(1);
      const root=process.env.OCTET_WEB_ROOT||__dirname;
      const file=path.join(root,asset);
      return route.fulfill({body:fs.readFileSync(fs.existsSync(file)?file:path.join(__dirname,asset)),contentType:asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://octet.test/');
    await page.waitForFunction(()=>document.getElementById('source-choice').options.length===5);
    assert.equal(await page.locator('#source-choice').inputValue(),'tidal');
    assert.equal(await page.locator('#source-choice').isEnabled(),true);
    assert.equal(await page.locator('#dsp-profile-name').textContent(),'Living room tuning');
    assert.equal(await page.locator('#now-playing-details').isHidden(),true);
    assert.equal(await page.locator('#toggle').isVisible(),true);
    assert.equal(await page.locator('#source-apply').count(),0);
    holdNextPoll=true;
    const oldPoll=page.evaluate(()=>refresh());
    await oldPollStarted;
    const switchRequest=page.waitForRequest(request=>new URL(request.url()).pathname==='/api/control/source');
    await page.selectOption('#source-choice','usb:iD14:mono-1');
    await switchRequest;
    assert.equal(await page.locator('#source-choice').isDisabled(),true);
    assert.match(await page.locator('#source-status').textContent(),/Switching audio source/);
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#source-choice').inputValue(),'usb:iD14:mono-1');
    releaseSwitch();
    await page.waitForFunction(()=>document.getElementById('source-current').textContent.includes('Input 1 · mono'));
    releasePoll();
    await oldPoll;
    assert.equal(await page.locator('#source-choice').inputValue(),'usb:iD14:mono-1');
    assert.equal(await page.locator('#source-choice').isEnabled(),true);
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
    reject=true;await page.selectOption('#source-choice','tidal');
    await page.waitForFunction(()=>document.getElementById('source-status').textContent.includes('Source change failed'));
    assert.equal(await page.locator('#source-choice').inputValue(),'usb:iD14:mono-1');
    reject=false;await page.selectOption('#source-choice','tidal');
    await page.waitForFunction(()=>document.getElementById('source-current').textContent==='Selected: Tidal');
    assert.equal(await page.locator('.transport').isVisible(),true);
    assert.equal(await page.getByRole('button',{name:'Library song',exact:true}).isDisabled(),false);
    assert.equal(await page.locator('#now-playing-details').isHidden(),true);
    state={...state,playback:'play'};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#song-title').textContent(),'Saved song');
    assert.equal(await page.locator('#song-album').textContent(),'Saved album');
    assert.equal(await page.locator('#now-playing-details').isVisible(),true);
    state={...state,muted:true,capture_stalled:true};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#now-playing-details').isHidden(),true);
    assert.equal(await page.locator('#playback-label').textContent(),'Audio stalled');
    await page.locator('#mute').click();
    await page.waitForFunction(()=>document.getElementById('player-error').textContent.includes('waiting for input audio'));
    state={...state,capture_stalled:false,dac_stalled:true,audio_health:{fault:'dac_output_stalled'}};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#now-playing-details').isHidden(),true);
    assert.match(await page.locator('#source-status').textContent(),/DAC output stopped advancing/);
    state={...state,dac_stalled:false,playback:'play',audio_health:{
      fault:'tidal_input_stalled',recovery:'retrying',attempts:2}};
    await page.evaluate(()=>refresh());
    assert.match(await page.locator('#source-status').textContent(),/Retrying Tidal input \(attempt 2\)/);
    assert.equal(await page.locator('#mute').textContent(),'Unmute');
    delete state.audio_health;
    state={...state,playback:'stop',capture_stalled:false};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#player-error').textContent(),'');
    assert.equal(await page.locator('#now-playing-details').isHidden(),true);
    assert.equal(await page.locator('#song-title').textContent(),'');
    assert.equal(await page.locator('#dsp-profile-name').textContent(),'Living room tuning');
    state={...state,ready:false,source_switching_available:false,source_switching_reason:'The current speaker configuration is not approved for playback.'};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#source-choice').isDisabled(),true);
    assert.equal(await page.locator('#source-status').textContent(),state.source_switching_reason);
    delete state.source_switching_reason;
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#source-status').textContent(),'Connect to the speaker DSP to change sources.');
    assert.doesNotMatch(await page.locator('#source-status').textContent(),/Output DSP controls/);
    state={...state,source:'airplay',source_label:'AirPlay',airplay:{active:true},current:null,playback:'stop'};
    nowPlaying={source:'airplay',state:'playing',title:'AirPlay song',artist:'AirPlay artist',album:'AirPlay album',artwork:'/api/now-playing/artwork/'+'a'.repeat(64)};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#song-title').textContent(),'AirPlay song');
    assert.equal(await page.locator('#song-artist').textContent(),'AirPlay artist');
    assert.equal(await page.locator('#song-album').textContent(),'AirPlay album');
    assert.match(await page.locator('#cover').getAttribute('src'),/\/api\/now-playing\/artwork\//);
    assert.equal(await page.locator('#cover').isVisible(),true);
    assert.deepEqual(writes,[{id:'usb:iD14:mono-1'},{id:'tidal'},{id:'tidal'}]);
    state={...state,source:'airplay',source_label:'AirPlay',airplay:{active:false}};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#now-playing-details').isHidden(),true);
    assert.equal(await page.locator('#cover').getAttribute('src'),null);
    assert.match(await page.locator('#source-status').textContent(),/waiting/);
    assert.equal(await page.locator('.transport').isVisible(),false);
    state={...state,airplay:{active:true},muted:false,volume:-18};
    await page.evaluate(()=>refresh());
    assert.match(await page.locator('#source-status').textContent(),/cast active/);
    assert.equal(await page.locator('#queue-count').textContent(),'0'); // AirPlay keeps the saved queue position.
    // Published live metadata replaces the fallback card only for the selected source.
    nowPlaying={source:'airplay',state:'playing',title:'AirPlay song',artist:'AirPlay artist',album:'AirPlay album',artwork:'/api/now-playing/artwork/'+'a'.repeat(64)};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#song-title').textContent(),'AirPlay song');
    assert.equal(await page.locator('#song-artist').textContent(),'AirPlay artist');
    assert.equal(await page.locator('#song-album').textContent(),'AirPlay album');
    assert.match(await page.locator('#cover').getAttribute('src'),/\/api\/now-playing\/artwork\//);
    assert.equal(await page.locator('#cover').isVisible(),true);
    nowPlaying={...nowPlaying,source:'bluetooth'};
    await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#song-title').textContent(),'AirPlay');
    assert.match(await page.locator('#source-status').textContent(),/cast active/);
    assert.equal(await page.locator('#song-album').textContent(),'');
    assert.equal(await page.locator('#cover').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log('Browser source flow passed: immediate selection, stale poll ignored, failed switch recovery, queue and level retention, Tidal return, unavailable reasons and mobile layout.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
