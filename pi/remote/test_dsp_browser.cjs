/* Run with Playwright available on NODE_PATH. No Pi or audio playback is used. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const settings = () => [
  {name:'hp', parameter:'freq', label:'High-pass', unit:'Hz', value:500},
  {name:'lp', parameter:'freq', label:'Low-pass', unit:'Hz', value:3500},
  {name:'attenuation', parameter:'gain', label:'Attenuation', unit:'dB', value:-12},
];
const status = {profile:'Test output DSP',volume:-36,max_db:-18,sample_rate:48000,muted:true,
  channels:Array.from({length:8},(_,i)=>({output:i+1,driver:'Output '+(i+1),input:i%2?'Right':'Left',
    available:true,eq_available:false,enabled:false,inverted:false,muted:i>3,role:'exciter',
    filters:[],always_active:[],always_active_controls:settings()}))};

(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.DSP_BROWSER_CHANNEL?{channel:process.env.DSP_BROWSER_CHANNEL}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1280,height:1000}});
    const errors=[],writes=[];let rejectSave=false;
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://octet.test/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/auto')return route.fulfill({json:{available:false,profiles:[]}});
      if(url.pathname==='/api/dsp')return route.fulfill({json:status});
      if(url.pathname==='/api/control/channel-always-active'){
        const body=route.request().postDataJSON();writes.push(body);
        if(rejectSave)return route.fulfill({status:503,json:{error:'DSP rejected the change; current settings were retained'}});
        for(const item of status.channels[body.output-1].always_active_controls){
          item.value=body.values[item.name];
          // Match the backend's private names after the first output edit.
          if(!item.name.startsWith('output_'))item.name='output_'+body.output+'_active_'+item.name;
        }
        return route.fulfill({json:{}});
      }
      const file=url.pathname==='/dsp'?'dsp.html':url.pathname.slice(1);
      if(!['dsp.html','dsp.js','auto.js','measurements.js','style.css'].includes(file))return route.fulfill({status:404,body:''});
      return route.fulfill({body:fs.readFileSync(path.join(__dirname,file)),
        contentType:file.endsWith('.html')?'text/html':file.endsWith('.js')?'text/javascript':'text/css'});
    });
    await page.goto('http://octet.test/dsp');
    await page.locator('.active-editor').last().waitFor({state:'attached'});
    assert.equal(await page.locator('.active-editor input').count(),24);
    const form=n=>page.getByRole('form',{name:'Always-active settings for output '+n,exact:true});
    const input=(n,label)=>form(n).getByRole('spinbutton',{name:label+' for output '+n,exact:true});
    for(const n of [1,2,5,8])await page.locator('details[data-key="guard-'+n+'"]').evaluate(node=>{node.open=true});
    await input(1,'High-pass in Hz').fill('700');
    await input(1,'Attenuation in dB').fill('-20.5');
    assert.equal(writes.length,0,'Typing must not change audio');
    // Rebuild during polling while another output changes; retain the unsaved draft.
    status.channels[1].inverted=true;
    await page.evaluate(()=>refresh());
    assert.equal(await input(1,'High-pass in Hz').inputValue(),'700');
    assert.equal(await input(1,'Attenuation in dB').inputValue(),'-20.5');
    await form(1).getByRole('button',{name:'Save output 1',exact:true}).click();
    await page.getByRole('status').filter({hasText:'Output 1 always-active settings saved.'}).waitFor();
    assert.deepEqual(writes[0],{output:1,values:{hp:700,lp:3500,attenuation:-20.5}});
    assert.equal(await input(1,'High-pass in Hz').inputValue(),'700');
    assert.equal(await input(2,'High-pass in Hz').inputValue(),'500');
    // Client validation keeps a crossed band and blank field out of the API.
    await input(5,'High-pass in Hz').fill('4000');
    await form(5).getByRole('button',{name:'Save output 5',exact:true}).click();
    assert.equal(writes.length,1);
    assert.match(await page.locator('#notice').textContent(),/High-pass must be below low-pass/);
    await input(5,'High-pass in Hz').fill('');
    await form(5).getByRole('button',{name:'Save output 5',exact:true}).click();
    assert.equal(writes.length,1);
    await input(5,'High-pass in Hz').fill('100');
    rejectSave=true;
    await form(5).getByRole('button',{name:'Save output 5',exact:true}).click();
    await page.locator('#notice').filter({hasText:'DSP rejected the change'}).waitFor();
    assert.equal(await input(5,'High-pass in Hz').inputValue(),'100');
    assert.equal(await form(5).getByRole('button').isEnabled(),true);
    assert.equal(status.channels[4].always_active_controls[0].value,500);
    await page.reload();
    await page.locator('details[data-key="guard-1"]').evaluate(node=>{node.open=true});
    assert.equal(await input(1,'Attenuation in dB').inputValue(),'-20.5');
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.deepEqual(errors,[]);
    if(process.env.DSP_SCREENSHOT)await page.screenshot({path:process.env.DSP_SCREENSHOT,fullPage:true});
    console.log('DSP browser checks passed: eight editors, explicit save, isolated values, polling drafts, validation, retry, reload, mobile layout.');
  } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
