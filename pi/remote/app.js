'use strict';
const $=id=>document.getElementById(id);
const volumeControl=window.OctetVolume.create($('volume'),$('volume-value'));
let state={}, volumeBusy=false, requestSerial=0, albumTracks=[];
let sourceBusy=false, sourceOptionsKey='';
function liveInput(){return Boolean(state.source&&state.source!=='tidal');}
function render(next){
  renderPlayer(next);
  renderSources(next);
  if(liveInput()){
    $('player-title').textContent='LIVE INPUT';$('playback-label').textContent=next.ready?(next.muted?'Muted':'Listening'):'Unavailable';
    const song=next.now_playing;
    if(song&&song.source===next.source&&song.title){
      $('song-title').textContent=song.title;
      $('song-artist').textContent=song.artist||next.source_label;
      $('song-album').textContent=song.album||'';
      if(song.artwork){if($('cover').getAttribute('src')!==song.artwork)$('cover').src=song.artwork;$('cover').hidden=false;$('cover-placeholder').hidden=true;}
      else{$('cover').hidden=true;$('cover-placeholder').hidden=false;}
    }else{
      $('song-title').textContent=next.source_label;$('song-artist').textContent=next.source==='airplay'?(next.airplay?.active?'AirPlay cast active.':'Waiting for an AirPlay cast.'):'Playing through your speaker DSP.';
      $('song-album').textContent='';$('cover').hidden=true;$('cover-placeholder').hidden=false;
    }
    if(next.player_error)notice();
  }else $('player-title').textContent='NOW PLAYING';
  $('seek').hidden=liveInput();document.querySelector('.time').hidden=liveInput();document.querySelector('.transport').hidden=liveInput();
  for(const button of document.querySelectorAll('.starts-playback, #play-album, .playlist-play')){
    button.disabled=liveInput()||playlistPlayBusy||button.dataset.empty==='true';
    if(liveInput())button.title='Choose Tidal as the audio source to play this track.';
  }
}
function renderSources(next){
  const select=$('source-choice'),sources=next.sources||[];
  const key=JSON.stringify(sources.map(source=>[source.id,source.label,source.available,source.group]));
  if(key!==sourceOptionsKey){
    const pending=select.value;select.replaceChildren();let group=null,lastGroup=null;
    for(const source of sources){
      if(source.group!==lastGroup){group=document.createElement('optgroup');group.label=source.group;select.append(group);lastGroup=source.group;}
      const option=document.createElement('option');option.value=source.id;option.textContent=source.label;option.disabled=!source.available;group.append(option);
    }
    select.value=sources.some(source=>source.id===pending)?pending:next.source;sourceOptionsKey=key;
  }
  if(!sourceBusy&&select.dataset.dirty!=='true')select.value=next.source;
  select.disabled=sourceBusy||!next.source_switching_available;
  $('source-apply').disabled=select.disabled||select.value===next.source||!sources.some(source=>source.id===select.value&&source.available);
  $('source-apply').textContent=sourceBusy?'Switching…':'Switch source';
  $('source-current').textContent='Selected: '+(next.source_label||'Unavailable');
  const chosen=sources.find(source=>source.id===select.value);
  $('source-note').textContent=chosen?.description||'Connect a USB audio interface to add line inputs.';
  if(!sourceBusy){
    $('source-status').textContent=!next.source_switching_available?(next.source_switching_reason||'Connect to the speaker DSP to change sources.'):!next.ready?'The selected source is unavailable. Reconnect it or choose Tidal.':liveInput()&&next.muted?'Input selected · muted. Unmute when ready.':'';
    if(next.source==='airplay')$('source-status').textContent=next.airplay?.error||(next.airplay?.preparing?'Preparing AirPlay · muted.':next.airplay?.active?'AirPlay cast active.':'AirPlay selected · waiting for a cast.');
    if(next.airplay?.error)$('source-status').textContent=next.airplay.error;
    if(next.dac_stalled)$('source-status').textContent='The speaker output is stalled (no sound will play). Reboot the Pi.';
    $('source-status').classList.toggle('error',!next.ready||Boolean(next.airplay?.error)||Boolean(next.dac_stalled));
  }
}
$('source-choice').onchange=()=>{$('source-choice').dataset.dirty='true';renderSources(state);};
$('source-form').onsubmit=async event=>{
  event.preventDefault();if(sourceBusy||$('source-apply').disabled)return;
  const id=$('source-choice').value;sourceBusy=true;renderSources(state);
  $('source-status').textContent='Switching audio source…';$('source-status').classList.remove('error');
  try{
    const next=await api('/api/control/source',{id});sourceBusy=false;$('source-choice').dataset.dirty='false';render(next);
    $('source-status').textContent='Source selected · muted. '+(id==='tidal'?'Press Play to resume your queue.':'Unmute when ready.');
  }catch(error){
    sourceBusy=false;$('source-choice').dataset.dirty='false';await refresh();renderSources(state);
    $('source-status').textContent=error.message;$('source-status').classList.add('error');
  }
};
let activePlaylist=null,playlistRows=[],playlistOffset=0,playlistHasMore=false,playlistsBusy=false,playlistPlayBusy=false,playlistLoadSerial=0;
const LIBRARY_CACHE_KEY='octet.tidal.library.v1',LIBRARY_VIEW_KEY='octet.tidal.view.v1',CACHE_FRESH_MS=5*60*1000;
function stored(key){try{return JSON.parse(localStorage.getItem(key));}catch{return null;}}
function persist(key,value){try{localStorage.setItem(key,JSON.stringify(value));}catch{/* Browsing still works when storage is unavailable or full. */}}
let libraryCache=stored(LIBRARY_CACHE_KEY);
if(!libraryCache||!Array.isArray(libraryCache.playlists)||typeof libraryCache.contents!=='object'||!libraryCache.contents)libraryCache={playlists:[],updatedAt:0,contents:{}};
function saveLibraryCache(){persist(LIBRARY_CACHE_KEY,libraryCache);}
function rememberLibrary(view,id){persist(LIBRARY_VIEW_KEY,{view,id});}
function fresh(entry){return entry&&Number.isFinite(entry.updatedAt)&&Date.now()-entry.updatedAt>=0&&Date.now()-entry.updatedAt<CACHE_FRESH_MS;}
let libraryEntry=null;
const libraryHistory=[];
function openLibrary(route){
  if(JSON.stringify(libraryEntry?.route)!==JSON.stringify(route)){
    if(libraryEntry){
      libraryEntry.scroll=window.scrollY;libraryEntry.search=libraryEntry.route.view==='search'?libraryEntry.route.query:$('search').value;
      libraryHistory.push(libraryEntry);
      if(libraryHistory.length>50)libraryHistory.shift();
    }else if(route.view==='playlist'){
      // A playlist restored after a reload still has a way back to its library.
      libraryHistory.push({route:{view:'playlists'},scroll:0,search:''});
    }
    libraryEntry={route,scroll:window.scrollY,search:$('search').value};
  }
  if(route.view==='search')$('search').value=route.query;
  libraryView(route.view);
  $('library-back').disabled=!libraryHistory.length;
}
async function libraryBack(){
  const entry=libraryHistory.pop();if(!entry)return;
  libraryEntry=entry;$('search').value=entry.search;
  const route=entry.route;
  if(route.view==='playlist')await loadPlaylist(route.id,false,entry.data);
  else if(route.view==='playlists')await loadPlaylists(true,false,entry.data);
  else if(route.view==='album')await loadAlbum(route.id,route.title,entry.data);
  else await loadTracks(route.view==='search'?route.query:null,entry.data);
  if(libraryEntry===entry)window.scrollTo({top:entry.scroll,behavior:'instant'});
}
const time=seconds=>{const n=Math.max(0,Math.floor(Number(seconds)||0));return Math.floor(n/60)+':'+String(n%60).padStart(2,'0')};
function notice(message='',error=false){$('notice').textContent=message;$('notice').classList.toggle('error',error)}
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw Error(result.error||'Could not complete that action');return result}
function renderPlayer(next){state=next;renderLevelControls(next);$('dsp-profile-name').textContent=next.profile;$('dsp-profile-description').textContent=next.profile_description;$('connection').textContent=next.ready?'● Octet online':'● DSP unavailable';$('connection').classList.toggle('online',next.ready);$('playback-label').textContent={play:'Playing',pause:'Paused',stop:'Stopped'}[next.playback]||'Stopped';const current=next.current;$('song-title').textContent=current?.title||'Room to listen.';$('song-artist').textContent=current?current.artist:'Choose a track to begin.';$('song-album').textContent=current?.album||'';if(current?.cover){if($('cover').getAttribute('src')!==current.cover)$('cover').src=current.cover;$('cover').hidden=false;$('cover-placeholder').hidden=true}else{$('cover').hidden=true;$('cover-placeholder').hidden=false}$('toggle').textContent=next.playback==='play'?'Ⅱ':'▶';$('toggle').setAttribute('aria-label',next.playback==='play'?'Pause':'Play');$('mute').textContent=next.muted?'Unmute':'Mute';$('mute').setAttribute('aria-pressed',String(next.muted));volumeControl.render(next,{busy:volumeBusy});if(document.activeElement!==$('seek')){$('seek').max=Math.max(1,next.duration);$('seek').value=next.elapsed}$('seek').disabled=!next.duration;$('elapsed').textContent=time(next.elapsed);$('duration').textContent=time(next.duration);renderQueue(next.queue||[]);if(next.player_error)notice(next.player_error,true)}
async function control(name,data={}){try{render(await api('/api/control/'+name,data));notice()}catch(e){notice(e.message,true)}}
function renderQueue(rows){
  const position=state.song_position??-1;
  const manual=state.manual_queue||rows.filter(row=>row.position>position&&row.origin==='queue');
  const autoplay=state.autoplay||rows.filter(row=>row.position>position&&row.origin!=='queue');
  $('queue-count').textContent=manual.length+autoplay.length;
  $('clear').disabled=!manual.length;$('clear').title='Clear songs queued by you';
  const queue=$('queue');queue.replaceChildren();
  function section(label,tracks,removable=false){
    if(!tracks.length)return;
    const heading=document.createElement('h3');heading.className='queue-heading';heading.textContent=label;queue.append(heading);
    for(const [index,row] of tracks.entries()){
      const div=document.createElement('div');div.className='queue-track'+(row.position===position?' current':'');
      const number=document.createElement('span');number.className='number';number.textContent=row.position===position?'♫':index+1;
      const button=document.createElement('button');button.className='starts-playback queue-title';button.disabled=liveInput();button.textContent=row.title;button.title=row.title+' · '+row.artist;
      button.onclick=()=>control('position',{position:row.position,song_id:row.song_id});div.append(number,button);
      if(removable){const remove=document.createElement('button');remove.className='queue-remove';remove.textContent='×';remove.setAttribute('aria-label','Remove '+row.title+' from queue');remove.onclick=()=>control('remove-queued',{song_id:row.song_id});div.append(remove);}
      queue.append(div);
    }
  }
  section('Now playing',state.current?[state.current]:[]);
  section('Queued by you',manual,true);
  const source=state.autoplay_source;
  section('Autoplay'+(source?.title?' · '+source.title:''),autoplay);
  if(!manual.length&&!autoplay.length){const p=document.createElement('p');p.className='empty';p.textContent='No upcoming songs. Use a track’s menu to queue it.';queue.append(p);}
}
function renderTracks(rows,playlistIdentifier=null,albumIdentifier=null){
  const root=$('tracks');root.replaceChildren();
  if(!rows.length){const p=document.createElement('p');p.className='empty';p.textContent=playlistIdentifier?'This playlist has no available tracks.':'No tracks here yet. Search for something you love.';root.append(p);return;}
  for(const [index,row] of rows.entries()){
    const div=document.createElement('div');div.className='track';
    const art=row.cover?document.createElement('img'):document.createElement('div');art.className='small-art';if(row.cover){art.src=row.cover;art.alt='';art.loading='lazy';}
    const info=document.createElement('div');info.className='track-info';
    const title=document.createElement('button');title.className='track-title starts-playback';title.disabled=liveInput();title.textContent=row.title;title.title='Play '+row.title;
    title.onclick=()=>playlistIdentifier?playPlaylist(playlistIdentifier,index,row.id):albumIdentifier?playAlbum(albumIdentifier,index,row.id):control('play',{id:row.id});
    const detail=document.createElement('p');detail.textContent=row.artist+' · '+row.album;info.append(title,detail);
    const duration=document.createElement('span');duration.className='track-time';duration.textContent=time(row.duration);
    const menu=document.createElement('details');menu.className='track-menu';
    const toggle=document.createElement('summary');toggle.textContent='⋯';toggle.setAttribute('aria-label','Queue options for '+row.title);toggle.title='Queue options';
    const choices=document.createElement('div');choices.className='queue-choices';
    for(const [placement,label] of [['next','Play next'],['end','Add to queue']]){
      const button=document.createElement('button');button.textContent=label;button.setAttribute('aria-label',label+': '+row.title);
      button.onclick=()=>{menu.open=false;control('enqueue',{id:row.id,placement});};choices.append(button);
    }
    menu.append(toggle,choices);div.append(art,info,duration,menu);root.append(div);
  }
}
async function loadTracks(query=null,saved=null){const serial=++requestSerial;openLibrary(query===null?{view:'favorites'}:{view:'search',query});albumTracks=[];$('play-album').hidden=true;notice(query?'Searching Tidal…':'Loading favorites…');try{const data=saved||await api(query===null?'/api/favorites':'/api/search?q='+encodeURIComponent(query));if(serial!==requestSerial)return;libraryEntry.data=data;renderTracks(data.tracks);const albums=data.albums||[];const fragment=document.createDocumentFragment();for(const album of albums){const div=document.createElement('div');div.className='track';const icon=document.createElement('div');icon.className='small-art';icon.textContent='◉';icon.style.display='grid';icon.style.placeItems='center';const info=document.createElement('div');info.className='track-info';const title=document.createElement('button');title.className='track-title';title.textContent=album.title;title.onclick=()=>loadAlbum(album.id,album.title);const detail=document.createElement('p');detail.textContent=album.artist+' · Album · '+album.tracks+' tracks';info.append(title,detail);const open=document.createElement('button');open.className='add';open.textContent='›';open.setAttribute('aria-label','Open album '+album.title);open.onclick=()=>loadAlbum(album.id,album.title);div.append(icon,info,open);fragment.append(div)}if(albums.length&&!data.tracks.length)$('tracks').replaceChildren(fragment);else $('tracks').prepend(fragment);$('result-label').textContent=query===null?data.tracks.length+' tracks':(data.tracks.length+albums.length)+' results';notice()}catch(e){if(serial!==requestSerial)return;renderTracks([]);notice(e.message,true)}}
async function loadAlbum(id,title,saved=null){const serial=++requestSerial;openLibrary({view:'album',id,title});albumTracks=[];$('play-album').hidden=true;notice('Loading '+title+'…');try{const data=saved||await api('/api/album?id='+encodeURIComponent(id));if(serial!==requestSerial)return;libraryEntry.data=data;albumTracks=data.tracks;renderTracks(albumTracks,null,id);$('play-album').hidden=!albumTracks.length;$('result-label').textContent=albumTracks.length+' tracks';notice(title)}catch(e){if(serial===requestSerial)notice(e.message,true)}}
function libraryView(view){
  if(view==='favorites'||view==='playlists')rememberLibrary(view);
  $('refresh-library').hidden=view!=='playlists'&&view!=='playlist';
  for(const [id,selected] of [['favorites',view==='favorites'],['playlists',view==='playlists'||view==='playlist']]){
    $(id).classList.toggle('active',selected);$(id).setAttribute('aria-pressed',String(selected));
  }
  $('playlist-header').hidden=view!=='playlist';$('more-playlists').hidden=true;
  if(view!=='playlist')activePlaylist=null;
  if(view!=='album'){$('play-album').hidden=true;albumTracks=[];}
}
function playlistBusy(value){
  playlistPlayBusy=value;
  for(const button of document.querySelectorAll('.starts-playback, #play-album, .playlist-play'))button.disabled=value||button.dataset.empty==='true'||liveInput();
}
function renderPlaylists(){
  const root=$('tracks');root.replaceChildren();
  if(!playlistRows.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='No saved playlists found in your Tidal account.';root.append(empty);}
  for(const playlist of playlistRows.slice().reverse()){
    const row=document.createElement('div');row.className='track playlist-row';
    const art=playlist.cover?document.createElement('img'):document.createElement('div');art.className='small-art';
    if(playlist.cover){art.src=playlist.cover;art.alt='';art.loading='lazy';}else art.textContent='≡';
    const info=document.createElement('div');info.className='track-info';
    const title=document.createElement('button');title.className='track-title';title.textContent=playlist.title;title.setAttribute('aria-label','Open playlist '+playlist.title);title.onclick=()=>loadPlaylist(playlist.id);
    const meta=document.createElement('p');meta.textContent=playlist.creator+' · '+playlist.tracks+' tracks';info.append(title,meta);
    const play=document.createElement('button');play.className='quiet-button playlist-play';play.textContent='Play all ▶';play.setAttribute('aria-label','Play all of '+playlist.title);play.dataset.empty=String(!playlist.tracks);play.disabled=playlistPlayBusy||!playlist.tracks||liveInput();play.onclick=()=>playPlaylist(playlist.id);
    row.append(art,info,play);root.append(row);
  }
  $('result-label').textContent=playlistRows.length+' playlists';$('more-playlists').hidden=!playlistHasMore;$('more-playlists').disabled=playlistsBusy;
}
async function loadPlaylists(reset=true,force=false,saved=null){
  if(playlistsBusy&&!reset)return;
  const serial=++requestSerial;playlistLoadSerial=serial;openLibrary({view:'playlists'});playlistsBusy=true;
  playlistRows=(saved||libraryCache.playlists).slice();playlistHasMore=false;renderPlaylists();
  if(saved||(!force&&fresh(libraryCache))){libraryEntry.data=playlistRows;playlistsBusy=false;notice();return;}
  $('more-playlists').disabled=true;notice(playlistRows.length?'Showing saved playlists · checking for updates…':'Loading Tidal playlists…');
  try{
    const rows=[],known=new Set();let offset=0;
    while(true){
      const data=await api('/api/playlists?offset='+offset);
      for(const row of data.playlists){if(!known.has(row.id)){rows.push(row);known.add(row.id);}}
      if(!data.has_more)break;
      if(!Number.isInteger(data.next_offset)||data.next_offset<=offset)throw Error('Could not load the complete playlist library');
      offset=data.next_offset;
    }
    libraryCache.playlists=rows;libraryCache.updatedAt=Date.now();saveLibraryCache();
    if(serial!==requestSerial)return;
    libraryEntry.data=rows;playlistRows=rows;playlistHasMore=false;renderPlaylists();notice();
  }catch(error){if(serial===requestSerial)notice((playlistRows.length?'Showing saved playlists. ':'')+error.message,true);}
  finally{if(serial===playlistLoadSerial)playlistsBusy=false;if(serial===requestSerial)$('more-playlists').disabled=false;}
}
function renderPlaylist(data,id){
  libraryEntry.data=data;
  activePlaylist=data;$('playlist-title').textContent=data.playlist.title;
  $('playlist-meta').textContent=data.tracks.length+' playable tracks'+(data.unavailable?' · '+data.unavailable+' unavailable omitted':'')+' · continues in playlist order';
  $('play-playlist').dataset.empty=String(!data.tracks.length);$('play-playlist').disabled=playlistPlayBusy||!data.tracks.length||liveInput();
  $('result-label').textContent=data.tracks.length+' tracks';renderTracks(data.tracks,id);
}
async function loadPlaylist(id,force=false,saved=null){
  const serial=++requestSerial;openLibrary({view:'playlist',id});activePlaylist=null;
  rememberLibrary('playlist',id);
  const cached=libraryCache.contents[id];
  $('playlist-title').textContent='Loading playlist…';$('playlist-meta').textContent='';$('play-playlist').disabled=true;$('play-playlist').dataset.empty='true';$('tracks').replaceChildren();notice('Loading the complete playlist…');
  if(saved){renderPlaylist(saved,id);notice();return;}
  if(cached&&cached.data?.playlist?.id===id&&Array.isArray(cached.data.tracks)){renderPlaylist(cached.data,id);notice('Showing saved playlist · checking for updates…');if(!force&&fresh(cached)){notice();return;}}
  try{
    const data=await api('/api/playlist?id='+encodeURIComponent(id)+'&refresh=1');if(serial!==requestSerial)return;
    libraryCache.contents[id]={updatedAt:Date.now(),data};
    for(const [oldId] of Object.entries(libraryCache.contents).sort((a,b)=>b[1].updatedAt-a[1].updatedAt).slice(12))delete libraryCache.contents[oldId];
    saveLibraryCache();renderPlaylist(data,id);notice();
  }catch(error){if(serial===requestSerial){if(!activePlaylist)$('playlist-title').textContent='Playlist unavailable';notice((activePlaylist?'Showing saved playlist. ':'')+error.message,true);}}
}
async function playPlaylist(id,position=0,track=null){
  if(playlistPlayBusy)return;playlistBusy(true);notice('Loading playlist autoplay…');
  try{
    const body={id,position};if(track!==null)body.track_id=track;
    render(await api('/api/control/play-playlist',body));notice('Playing in playlist order. The following tracks play automatically.');
    rememberLibrary('playlist',id);
  }catch(error){notice(error.message,true);}finally{playlistBusy(false);}
}
$('playlists').onclick=()=>loadPlaylists();
$('refresh-library').onclick=()=>activePlaylist?loadPlaylist(activePlaylist.playlist.id,true):loadPlaylists(true,true);
$('more-playlists').onclick=()=>loadPlaylists(false);
$('library-back').onclick=libraryBack;
$('play-playlist').onclick=()=>{if(activePlaylist)playPlaylist(activePlaylist.playlist.id);};

async function playAlbum(id,position=0,track=null){
  if(playlistPlayBusy)return;playlistBusy(true);notice('Loading album autoplay…');
  try{const body={id,position};if(track!==null)body.track_id=track;render(await api('/api/control/play-album',body));notice('Playing in album order.');}
  catch(error){notice(error.message,true);}finally{playlistBusy(false);}
}
$('play-album').onclick=()=>{if(albumTracks.length&&libraryEntry?.route.view==='album')playAlbum(libraryEntry.route.id);};
$('search-form').onsubmit=e=>{e.preventDefault();const query=$('search').value.trim();if(query)loadTracks(query)};
$('favorites').onclick=()=>loadTracks();
$('toggle').onclick=()=>control(state.playback==='play'?'pause':'resume');
$('previous').onclick=()=>control('previous');$('next').onclick=()=>control('next');$('stop').onclick=()=>control('stop');$('clear').onclick=()=>control('clear-queue');$('mute').onclick=()=>control('mute',{muted:!state.muted});
$('volume').oninput=()=>{volumeBusy=true;volumeControl.preview()};
$('volume').onchange=async()=>{const value=volumeControl.value();await control('volume',{db:value});volumeBusy=false;refresh()};
$('seek').onchange=()=>control('seek',{seconds:Number($('seek').value)});
async function refresh(){if(sourceBusy)return;try{const next=await api('/api/status');if(next.source&&next.source!=='tidal')next.now_playing=(await api('/api/now-playing').catch(()=>({song:null}))).song;if(!sourceBusy)render(next)}catch(e){$('connection').textContent='● Reconnecting…';$('connection').classList.remove('online')}}
refresh();
const remembered=stored(LIBRARY_VIEW_KEY);
if(remembered?.view==='playlist'&&typeof remembered.id==='string')loadPlaylist(remembered.id);
else if(remembered?.view==='playlists')loadPlaylists();
else loadTracks();
setInterval(refresh,2500);
function renderLevelControls(next){$('unlock-level').textContent=next.level_unlocked?'Lock standard range':'Unlock higher levels';$('unlock-level').setAttribute('aria-pressed',String(next.level_unlocked));$('level-range-note').textContent=next.level_unlocked?'Higher range unlocked: up to 0 dB. Unlocking does not change the volume.':'Standard range: −60 to −18 dB.'}
$('unlock-level').onclick=async()=>{try{render(await api('/api/control/unlock',{enabled:!state.level_unlocked}));notice(state.level_unlocked?'Higher levels unlocked. The listening level has not changed.':'Standard listening range restored.')}catch(error){notice(error.message,true)}};
