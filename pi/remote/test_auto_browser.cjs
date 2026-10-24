const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[],writes=[];
    page.on('pageerror',e=>errors.push(e.message));
    let auto={available:true,status:'idle',message:'Ready for measurement',profiles:[],audio_owned:false,running:false,selected_pairs:['a','sub','tweeter']};
    await page.route('http://octet.test/**',async route=>{
      const p=new URL(route.request().url()).pathname;
      if(p==='/api/auto')return route.fulfill({json:auto});
      if(p==='/api/dsp')return route.fulfill({json:{profile:'Manual',volume:-36,max_db:-18,sample_rate:48000,muted:true,channels:[]}});
      if(p.startsWith('/api/control/auto-')){
        const body=route.request().postDataJSON();writes.push([p,body]);
        if(p.endsWith('start'))auto={...auto,status:'running',stage:'levels',message:'L_a: −54 dBFS',audio_owned:true,running:true,peak_db:-42};
        else if(p.endsWith('stop'))auto={...auto,status:'cancelled',message:'Stopped',running:false};
        else if(p.endsWith('restore'))auto={...auto,status:'idle',message:'Previous settings restored',audio_owned:false};
        else if(p.endsWith('select'))auto={...auto,active:auto.profiles[0],message:'Saved profile applied'};
        return route.fulfill({json:auto});
      }
      const file=p==='/dsp'?'dsp.html':p.slice(1);
      if(!['dsp.html','dsp.js','auto.js','measurements.js','style.css'].includes(file))return route.fulfill({status:404,body:''});
      return route.fulfill({body:fs.readFileSync(path.join(__dirname,file)),contentType:file.endsWith('html')?'text/html':file.endsWith('js')?'text/javascript':'text/css'});
    });
    await page.goto('http://octet.test/dsp');
    await page.getByRole('button',{name:'Start Auto mode'}).waitFor();
    await page.waitForFunction(()=>!document.getElementById('auto-start').disabled);
    assert.equal(await page.locator('input[name="auto-pair"][value="b"]').isChecked(),false);
    await page.locator('input[name="auto-pair"][value="a"]').uncheck();
    assert.equal(await page.locator('#auto-start').isDisabled(),true,'An exciter pair is required');
    await page.locator('input[name="auto-pair"][value="a"]').check();
    await page.getByRole('button',{name:'Start Auto mode'}).click();
    assert.equal(writes.length,0,'Mic readiness must be required');
    await page.locator('#auto-ready').check();await page.locator('#auto-name').fill('Chair');
    await page.getByRole('button',{name:'Start Auto mode'}).click();
    await page.waitForFunction(()=>document.getElementById('auto-status').textContent.includes('L_a'));
    assert.deepEqual(writes[0][1],{name:'Chair',microphone_ready:true,selected_pairs:['a','sub','tweeter']});
    assert.equal(await page.locator('input[name="auto-pair"][value="a"]').isDisabled(),true);
    assert.equal(await page.locator('#volume').isDisabled(),true);
    await page.getByRole('button',{name:'Stop tones',exact:true}).last().click();
    await page.waitForFunction(()=>document.getElementById('auto-status').textContent==='Stopped');
    auto.profiles=[{id:'saved',name:'Chair',created:'2026-10-20'}];
    await page.waitForFunction(()=>!document.getElementById('auto-restore').disabled);
    await page.getByRole('button',{name:'Restore before tuning'}).click();
    await page.waitForFunction(()=>!document.getElementById('auto-select').disabled);
    await page.getByRole('button',{name:'Apply profile',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('auto-status').textContent==='Saved profile applied');
    assert.equal(writes.at(-1)[1].id,'saved');
    await page.evaluate(()=>window.renderTunedOutputs({tuning:{selected_pairs:['a','sub','tweeter'],chosen:Object.fromEntries(['L','R'].map(side=>[side,{low:400,high:4200,order:4,gains:{a:0,sub:-3,tweeter:-6},delays:{a:0,sub:1,tweeter:2},polarity:{a:1,sub:1,tweeter:1}}]))}}));
    assert.equal(await page.locator('#channels article').count(),8);
    assert.match(await page.locator('#channels article').nth(2).innerText(),/output muted/);
    assert.match(await page.locator('#channels article').nth(3).innerText(),/output muted/);
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile layout overflows');
    assert.deepEqual(errors,[]);
    console.log('Auto mode browser checks passed: mic readiness, start, progress, output lock, stop, recovery, recall, mobile layout.');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
