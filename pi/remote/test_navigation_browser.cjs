/* Exercise the music page without contacting Tidal or changing playback. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const track=i=>({id:String(i),title:'Song '+i,artist:'Artist',album:'Album',duration:180});
const playlist={id:'saved',title:'Evening playlist',creator:'Listener',tracks:40};
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[],writes=[],requests=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://octet.test/**',async route=>{
      const url=new URL(route.request().url()),p=url.pathname;
      if(route.request().method()==='POST')writes.push(p);
      if(p.startsWith('/api/'))requests.push(p);
      if(p==='/api/status')return route.fulfill({json:{ready:true,source:'tidal',source_label:'Tidal',sources:[],source_switching_available:true,profile:'Manual',volume:-36,max_db:-18,muted:true,playback:'stop',queue:[]}});
      if(p==='/api/favorites')return route.fulfill({json:{tracks:Array.from({length:40},(_,i)=>track(i))}});
      if(p==='/api/playlists')return route.fulfill({json:{playlists:[playlist],has_more:false}});
      if(p==='/api/playlist')return route.fulfill({json:{playlist,tracks:Array.from({length:40},(_,i)=>track(i)),unavailable:0}});
      if(p==='/api/search')return route.fulfill({json:{tracks:Array.from({length:40},(_,i)=>track(i)),albums:[{id:'album',title:'Search album',artist:'Artist',tracks:40}]}});
      if(p==='/api/album')return route.fulfill({json:{tracks:Array.from({length:40},(_,i)=>track(i))}});
      if(p.startsWith('/api/'))return route.fulfill({json:{available:false,inputs:[],outputs:[]}});
      const file=p==='/'?'index.html':p.slice(1);
      if(!['index.html','app.js','volume.js','style.css','input-levels.js'].includes(file))return route.fulfill({status:404,body:''});
      return route.fulfill({body:fs.readFileSync(path.join(__dirname,file)),contentType:file.endsWith('html')?'text/html':file.endsWith('js')?'text/javascript':'text/css'});
    });
    for(const width of [1280,390]){
      await page.setViewportSize({width,height:1000});
      await page.goto('http://octet.test/');
      // Reload can restore the playlist library from the previous pass.
      await page.locator('#favorites').click();
      await page.locator('#tracks .track').first().waitFor();
      await page.locator('#playlists').click();
      await page.getByRole('button',{name:'Open playlist Evening playlist',exact:true}).click();
      await page.getByRole('heading',{name:'Evening playlist',exact:true}).waitFor();
      await page.locator('#library-back').click();
      assert.equal(await page.locator('#playlist-header').isVisible(),false);
      await page.getByRole('button',{name:'Open playlist Evening playlist',exact:true}).waitFor();
      await page.locator('#search').fill('favorite artist');
      await page.locator('#search-form button').click();
      await page.getByRole('button',{name:'Open album Search album',exact:true}).waitFor();
      await page.evaluate(()=>window.scrollTo(0,800));
      const scroll=await page.evaluate(()=>scrollY);
      await page.locator('#tracks .track-title').first().click();
      await page.locator('#play-album').waitFor();
      const count=requests.filter(p=>p==='/api/search').length;
      await page.locator('#library-back').click();
      await page.getByRole('button',{name:'Open album Search album',exact:true}).waitFor();
      assert.equal(await page.locator('#search').inputValue(),'favorite artist');
      assert.equal(await page.evaluate(()=>scrollY),scroll);
      assert.equal(requests.filter(p=>p==='/api/search').length,count);
      assert.equal(await page.locator('#play-album').isVisible(),false);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Layout overflows at '+width);
      await page.locator('#library-back').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(__dirname,'../../out/navigation-'+width+'.png')});
    }
    assert.deepEqual(writes,[],'Browsing must not send playback controls');
    assert.deepEqual(errors,[]);
    console.log('Music navigation passed on desktop and mobile: playlist back, album back, search/query/scroll restoration, no playback changes, no layout overflow.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
