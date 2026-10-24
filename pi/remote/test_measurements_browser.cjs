const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[],writes=[],reads=[];
  page.on('pageerror',e=>errors.push(e.message));
  const a='a'.repeat(32),b='b'.repeat(32),manual='before-20261020';
  const freq=[40,80,160,320,640,1280,2560,5120,10240,18000];
  const response=id=>({available:true,profile:{id,name:id===a?'Chair':'Sofa'},message:'Saved measurements for this profile. Viewing graphs does not apply the profile.',notes:'Relative dB; final system curves are measured.',
   systems:['L','R'].map(side=>({side,freq,before:[-15,-6,-2,0,4,1,2,-1,-5,-10],after:[-6,-2,0,0,1,0,0,-1,-2,-4],target:[4,3,2,1,0,0,-1,-2,-3,-4],crossover:{low:400,high:4200}})),
   drivers:[1,2].map(n=>({id:'driver'+n,output:n,model:n===1?'EX30':'EX32',side:n===1?'L':'R',freq,measured:[-15,-6,-2,0,4,1,2,-1,-5,-10],predicted:[-20,-15,-4,-2,-1,-2,-3,-6,-10,-15],phase:[170,-170,60,-10,-40,-80,-120,-170,175,120],predicted_phase:[100,60,20,-40,-70,-120,-175,170,120,70],delay_ms:n*.4,polarity:n===1?1:-1,attenuation_db:-n*3}))});
  await page.route('http://octet.test/**',async route=>{
   const p=new URL(route.request().url()).pathname;
   if(p==='/api/auto')return route.fulfill({json:{available:true,status:'idle',running:false,audio_owned:false,message:'Ready',active:{id:a},profiles:[{id:a,name:'Chair'},{id:b,name:'Sofa'},{id:manual,name:'Before tuning'}]}});
   if(p==='/api/dsp')return route.fulfill({json:{profile:'Manual',volume:-36,max_db:-18,muted:true,channels:[]}});
   if(p.startsWith('/api/control/')){writes.push(p);return route.fulfill({json:{}});}
   if(p.startsWith('/api/auto/profiles/')){const id=p.split('/')[4];reads.push(id);return route.fulfill({json:id===manual?{profile:{id,name:'Before tuning'},available:false,message:'This is a manual settings backup. No acoustic measurements were recorded for it.',systems:[],drivers:[]}:response(id)});}
   const file=p==='/dsp'?'dsp.html':p.slice(1);
   if(!['dsp.html','dsp.js','auto.js','measurements.js','style.css'].includes(file))return route.fulfill({status:404,body:''});
   return route.fulfill({body:fs.readFileSync(path.join(__dirname,file)),contentType:file.endsWith('html')?'text/html':file.endsWith('js')?'text/javascript':'text/css'});
  });
  await page.goto('http://octet.test/dsp');await page.waitForFunction(()=>document.getElementById('auto-profiles').options.length===3);
  await page.selectOption('#auto-profiles',b);
  await page.getByRole('button',{name:'View measurements',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.measurement-chart').length===4);
  assert.equal(await page.locator('#measurement-title').textContent(),'Measurements · Sofa');
  assert.equal(reads[0],b);assert.deepEqual(writes,[],'Viewing a profile must not apply it');
  await page.selectOption('#measurement-driver','driver2');
  assert((await page.locator('#measurement-settings').textContent()).includes('inverted'));
  const graph=page.locator('.measurement-chart>svg').last();await graph.focus();await page.keyboard.press('ArrowRight');
  assert((await page.locator('.measurement-reading').last().textContent()).includes('80 Hz'));
  await page.screenshot({path:'build/auto-release/measurement-graphs.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.selectOption('#auto-profiles',manual);
  await page.waitForFunction(()=>document.getElementById('measurement-status').textContent.includes('manual settings backup'));
  assert.equal(await page.locator('.measurement-chart').count(),0);
  await page.getByRole('button',{name:'Close graphs'}).click();assert(await page.locator('#profile-measurements').isHidden());
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  console.log('Measurement viewer checks passed: selected profile, system/driver/phase plots, keyboard inspection, empty backups, mobile layout and no audio mutations.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
