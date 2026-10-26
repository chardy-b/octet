/* Browser library behavior without audio playback or a live Tidal account. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
const CACHE = 'octet.tidal.library.v1', VIEW = 'octet.tidal.view.v1';
class Element {
  constructor() { this.children=[];this.dataset={};this.style={};this.hidden=false;this.textContent='';this.classList={toggle(){},add(){},remove(){}};this.attributes={}; }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children=children; }
  setAttribute(key,value) { this.attributes[key]=value; }
  getAttribute(key) { return this.attributes[key]; }
}
function boot(cache, view, blocked=false) {
  const elements=new Map(), storage=new Map(), requests=[];
  if(cache)storage.set(CACHE,JSON.stringify(cache));
  if(view)storage.set(VIEW,JSON.stringify(view));
  const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},createDocumentFragment(){return new Element();},querySelectorAll(){return [];}};
  const context=vm.createContext({document,console,window:{scrollY:0,scrollTo({top}){this.scrollY=top;}},setInterval(){},localStorage:{getItem(key){if(blocked)throw Error('blocked');return storage.get(key)||null;},setItem(key,value){if(blocked)throw Error('quota');storage.set(key,value);}},fetch(url){return new Promise((resolve,reject)=>requests.push({url,resolve(value){resolve({ok:true,json:async()=>value});},reject}));}});
  vm.runInContext(source,context);
  return {elements,storage,requests,run(code){return vm.runInContext(code,context);}};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const playlist=id=>({id,title:'Playlist '+id,creator:'Listener',tracks:1});
const tracks=id=>({playlist:playlist(id),tracks:[{id:'123',title:'Song',artist:'Artist',album:'Album',duration:30}],unavailable:0});
const freshCache=()=>({playlists:[playlist('a'),playlist('b')],updatedAt:Date.now(),contents:{}});
const titles=app=>app.elements.get('tracks').children.map(row=>row.children[1].children[0].textContent);
(async()=>{
  const list=boot(freshCache(),{view:'playlists'});
  assert.deepEqual(titles(list),['Playlist b','Playlist a']);
  assert.equal(list.requests.filter(r=>r.url.includes('/api/playlists')).length,0);

  const paged=boot(null,{view:'playlists'});
  paged.requests.find(r=>r.url==='/api/playlists?offset=0').resolve({playlists:[playlist('a'),playlist('b')],has_more:true,next_offset:2});
  await flush();
  paged.requests.find(r=>r.url==='/api/playlists?offset=2').resolve({playlists:[playlist('c')],has_more:false,next_offset:3});
  await flush();
  assert.deepEqual(titles(paged),['Playlist c','Playlist b','Playlist a']);
  assert.equal(JSON.parse(paged.storage.get(CACHE)).playlists.length,3);

  const cache=freshCache();cache.contents.a={data:tracks('a'),updatedAt:Date.now()};
  const restored=boot(cache,{view:'playlist',id:'a'});
  assert.equal(restored.elements.get('playlist-title').textContent,'Playlist a');
  assert.equal(restored.requests.filter(r=>r.url.startsWith('/api/playlist?')).length,0);
  assert.equal(restored.requests.filter(r=>r.url.startsWith('/api/control/')).length,0);

  cache.contents.a.updatedAt=0;
  const stale=boot(cache,{view:'playlist',id:'a'});
  assert.equal(stale.elements.get('playlist-title').textContent,'Playlist a');
  stale.requests.find(r=>r.url.startsWith('/api/playlist?')).reject(Error('offline'));
  await flush();
  assert.equal(stale.elements.get('playlist-title').textContent,'Playlist a');
  assert.match(stale.elements.get('notice').textContent,/Showing saved playlist/);

  const refreshing=restored.run("loadPlaylist('a',true)");
  restored.requests.find(r=>r.url.startsWith('/api/playlist?')).resolve(tracks('a'));
  await refreshing;
  assert.equal(JSON.parse(restored.storage.get(VIEW)).id,'a');

  const blocked=boot(null,null,true);
  blocked.requests.find(r=>r.url==='/api/favorites').resolve({tracks:[]});
  await flush();
  const loading=blocked.run('loadPlaylists()');
  blocked.requests.find(r=>r.url.startsWith('/api/playlists?')).resolve({playlists:[playlist('a')],has_more:false,next_offset:1});
  await loading;
  assert.deepEqual(titles(blocked),['Playlist a']);
  assert.equal(restored.elements.get('library-back').disabled,false);
  await restored.run('libraryBack()');
  assert.deepEqual(titles(restored),['Playlist b','Playlist a']);
  assert.equal(restored.elements.get('library-back').disabled,true);

  const search=list.run("loadTracks('jazz')");
  list.requests.find(r=>r.url==='/api/search?q=jazz').resolve({tracks:[],albums:[{id:'42',title:'Jazz album',artist:'Artist',tracks:1}]});
  await search;
  list.run("window.scrollY=320; $('search').value='changed draft'");
  const album=list.run("loadAlbum('42','Jazz album')");
  list.requests.find(r=>r.url==='/api/album?id=42').resolve({tracks:tracks('a').tracks});
  await album;
  const count=list.requests.length;
  await list.run('libraryBack()');
  assert.equal(list.elements.get('search').value,'jazz');
  assert.equal(list.run('window.scrollY'),320);
  assert.equal(list.requests.length,count);
  assert.equal(list.elements.get('play-album').hidden,true);
  assert.equal(list.run('libraryHistory.length'),1);
  const pending=list.run("loadAlbum('99','Slow album')");
  await list.run('libraryBack()');
  list.requests.find(r=>r.url==='/api/album?id=99').resolve({tracks:tracks('b').tracks});
  await pending;
  assert.equal(list.run('libraryEntry.route.view'),'search');
  assert.equal(list.elements.get('play-album').hidden,true);
  assert.equal(list.requests.filter(r=>r.url.startsWith('/api/control/')).length,0);
  const html=fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
  assert.match(html,/id="library-back"[^>]*aria-label="Go back to the previous library view"/);
  assert.doesNotMatch(html,/id="playlist-back"/);
  console.log('Library history: reload fallback, search/album back, scroll, cached results, stale response and no playback passed.');
  console.log('6 browser cache checks passed: cached reverse order, full pagination, restored playlist without playback, offline fallback, forced refresh, unavailable storage.');
})().catch(error=>{console.error(error);process.exitCode=1;});
