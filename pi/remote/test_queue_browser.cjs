/* Verify queue controls and album context requests without contacting Tidal. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const song=(id,position,origin='autoplay')=>({id:String(id),title:'Song '+id,artist:'Artist',album:'Album',duration:180,position,origin,song_id:String(100+position)});
const current=song(1,0),manual=[song(8,1,'queue'),song(8,2,'queue')],autoplay=[song(2,3),song(3,4)];
const status={ready:true,source:'tidal',source_label:'Tidal',sources:[],source_switching_available:true,profile:'Manual',volume:-36,max_db:-18,muted:false,playback:'play',song_position:0,current,queue:[current,...manual,...autoplay],manual_queue:manual,autoplay,autoplay_source:{kind:'playlist',title:'Evening playlist'}};
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage(),errors=[],writes=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://octet.test/**',async route=>{
      const request=route.request(),p=new URL(request.url()).pathname;
      if(request.method()==='POST'){
        writes.push({path:p,body:request.postDataJSON()});
        if(p==='/api/control/clear-queue')status.manual_queue=[];
        return route.fulfill({json:status});
      }
      if(p==='/api/status')return route.fulfill({json:status});
      if(p==='/api/favorites')return route.fulfill({json:{tracks:[song(4),song(5)]}});
      if(p==='/api/album')return route.fulfill({json:{tracks:[song(10),song(11),song(12)],album:{id:'99',title:'An album'}}});
      if(p==='/api/playlists')return route.fulfill({json:{playlists:[{id:'saved',title:'Evening playlist',creator:'Listener',tracks:3}],has_more:false}});
      if(p==='/api/playlist')return route.fulfill({json:{playlist:{id:'saved',title:'Evening playlist'},tracks:[song(1),song(2),song(3)]}});
      if(p.startsWith('/api/'))return route.fulfill({json:{available:false,inputs:[],outputs:[]}});
      const file=p==='/'?'index.html':p.slice(1);
      if(!['index.html','app.js','volume.js','style.css','input-levels.js'].includes(file))return route.fulfill({status:404,body:''});
      return route.fulfill({body:fs.readFileSync(path.join(__dirname,file)),contentType:file.endsWith('html')?'text/html':file.endsWith('js')?'text/javascript':'text/css'});
    });
    for(const width of [1280,390]){
      status.manual_queue=manual;
      await page.setViewportSize({width,height:1000});
      await page.goto('http://octet.test/');
      await page.locator('#favorites').click();
      await page.locator('#tracks .track').first().waitFor();
      await page.waitForFunction(()=>document.querySelectorAll('#queue .queue-heading').length===3);
      assert.deepEqual(await page.locator('#queue .queue-heading').allTextContents(),['Now playing','Queued by you','Autoplay · Evening playlist']);
      assert.equal(await page.locator('#queue-count').textContent(),'4');
      await page.locator('#tracks summary').first().click();
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/control/enqueue')),page.getByRole('button',{name:'Play next: Song 4',exact:true}).click()]);
      await page.waitForFunction(()=>!document.querySelector('#tracks details').open);
      assert.deepEqual(writes.at(-1),{path:'/api/control/enqueue',body:{id:'4',placement:'next'}});
      await page.locator('#tracks summary').first().click();
      assert(await page.getByRole('button',{name:'Add to queue: Song 4',exact:true}).isVisible());
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Menu overflows at '+width);
      await page.screenshot({path:path.join(__dirname,'../../out/queue-'+width+'.png'),fullPage:true});
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/control/enqueue')),page.getByRole('button',{name:'Add to queue: Song 4',exact:true}).click()]);
      assert.deepEqual(writes.at(-1),{path:'/api/control/enqueue',body:{id:'4',placement:'end'}});
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/control/remove-queued')),page.getByRole('button',{name:'Remove Song 8 from queue',exact:true}).nth(1).click()]);
      await page.waitForFunction(()=>document.querySelector('#queue-count').textContent==='4');
      assert.deepEqual(writes.at(-1),{path:'/api/control/remove-queued',body:{song_id:'102'}});
      await page.locator('#clear').click();
      await page.waitForFunction(()=>document.querySelector('#queue-count').textContent==='2');
      assert.deepEqual(writes.at(-1),{path:'/api/control/clear-queue',body:{}});
      assert.equal(await page.locator('#clear').isDisabled(),true);
      assert.equal(await page.locator('#queue .queue-heading').last().textContent(),'Autoplay · Evening playlist');
      await page.evaluate(()=>loadAlbum('99','An album'));
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/control/play-album')),page.locator('#tracks .track-title').nth(1).click()]);
      assert.deepEqual(writes.at(-1),{path:'/api/control/play-album',body:{id:'99',position:1,track_id:'11'}});
      await page.locator('#play-album').click();
      await page.waitForFunction(()=>!document.querySelector('#play-album').disabled);
      assert.deepEqual(writes.at(-1),{path:'/api/control/play-album',body:{id:'99',position:0}});
      await page.locator('#playlists').click();
      await page.getByRole('button',{name:'Open playlist Evening playlist',exact:true}).click();
      await page.locator('#tracks .track-title').nth(1).click();
      await page.waitForFunction(()=>!document.querySelector('#play-playlist').disabled);
      assert.deepEqual(writes.at(-1),{path:'/api/control/play-playlist',body:{id:'saved',position:1,track_id:'2'}});
    }
    assert.deepEqual(errors,[]);
    console.log('Queue browser checks passed on desktop/mobile: next/end requests, queue sections, duplicate removal, clear preserving autoplay, album and playlist context selection, no layout overflow.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
