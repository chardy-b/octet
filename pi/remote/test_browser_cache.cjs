/* Browser library behavior without audio playback or a live Tidal account. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
const CACHE = 'octet.tidal.library.v1', VIEW = 'octet.tidal.view.v1';
class Element {
  constructor() { this.children=[];this.dataset={};this.style={};this.hidden=false;this.textContent='';this.classList={toggle(){},add(){},remove(){}};this.attributes={}; }
  addEventListener() {}
  append(...children) { this.children.push(...children.flatMap(child=>child.fragment?child.children:[child])); }
  prepend(...children) { this.children.unshift(...children.flatMap(child=>child.fragment?child.children:[child])); }
  replaceChildren(...children) { this.children=children.flatMap(child=>child.fragment?child.children:[child]); }
  setAttribute(key,value) { this.attributes[key]=value; }
  getAttribute(key) { return this.attributes[key]; }
}
function boot(cache, view, blocked=false) {
  const elements=new Map(), storage=new Map(), requests=[];
  if(cache)storage.set(CACHE,JSON.stringify(cache));
  if(view)storage.set(VIEW,JSON.stringify(view));
  const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},createDocumentFragment(){const fragment=new Element();fragment.fragment=true;return fragment;},querySelectorAll(){return [];}};
  const window={scrollY:0,scrollTo({top}){this.scrollY=top;}};
  const context=vm.createContext({document,window,console,setInterval(){},localStorage:{getItem(key){if(blocked)throw Error('blocked');return storage.get(key)||null;},setItem(key,value){if(blocked)throw Error('quota');storage.set(key,value);}},fetch(url){return new Promise((resolve,reject)=>requests.push({url,resolve(value){resolve({ok:true,json:async()=>value});},reject}));}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'volume.js'),'utf8'),context);
  vm.runInContext(source,context);
  return {elements,storage,requests,window,run(code){return vm.runInContext(code,context);}};
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

  // Back restores list position immediately, without Tidal requests or playback.
  const navCache=freshCache();navCache.contents.a={data:tracks('a'),updatedAt:Date.now()};
  const nav=boot(navCache,{view:'playlists'});
  assert.equal(nav.elements.get('library-back').disabled,true);
  nav.window.scrollY=420;
  await nav.run("loadPlaylist('a')");
  assert.equal(nav.elements.get('library-back').disabled,false);
  await nav.elements.get('library-back').onclick();
  assert.deepEqual(titles(nav),['Playlist b','Playlist a']);
  assert.equal(nav.window.scrollY,420);
  assert.equal(nav.elements.get('playlist-header').hidden,true);
  assert.equal(nav.elements.get('library-back').disabled,true);
  assert.equal(nav.requests.filter(r=>r.url!=='/api/status').length,0);

  // A playlist restored on page load also has a parent, even with an empty cache.
  await restored.elements.get('library-back').onclick();
  assert.deepEqual(titles(restored),['Playlist b','Playlist a']);
  assert.equal(restored.elements.get('library-back').disabled,true);

  // Search -> album -> search -> playlists retains query/results/scroll offline.
  const search=nav.run("loadTracks('old query')");
  nav.requests.find(r=>r.url==='/api/search?q=old%20query').resolve({tracks:[],albums:[{id:'album',title:'Test album',artist:'Artist',tracks:1}]});
  await search;nav.window.scrollY=640;
  const album=nav.run("loadAlbum('album','Test album')");
  nav.requests.find(r=>r.url==='/api/album?id=album').resolve({tracks:tracks('a').tracks});
  await album;
  assert.equal(nav.elements.get('play-album').hidden,false);
  nav.elements.get('search').value='unfinished edit';
  const count=nav.requests.length;
  await nav.elements.get('library-back').onclick();
  assert.equal(nav.elements.get('search').value,'old query');
  assert.deepEqual(titles(nav),['Test album']);
  assert.equal(nav.window.scrollY,640);
  assert.equal(nav.elements.get('play-album').hidden,true);
  await nav.elements.get('library-back').onclick();
  assert.deepEqual(titles(nav),['Playlist b','Playlist a']);
  assert.equal(nav.requests.length,count);

  // Going back during a request prevents its late response/error replacing the view.
  const pending=nav.run("loadAlbum('slow','Slow album')");
  await nav.elements.get('library-back').onclick();
  nav.requests.find(r=>r.url==='/api/album?id=slow').reject(Error('late error'));
  await pending;
  assert.deepEqual(titles(nav),['Playlist b','Playlist a']);
  assert.equal(nav.elements.get('notice').textContent,'');
  console.log('Browser cache and navigation checks passed: cache, pagination, offline fallback, refresh, playlist parents, search/album back, scroll restoration and late requests.');
})().catch(error=>{console.error(error);process.exitCode=1;});
