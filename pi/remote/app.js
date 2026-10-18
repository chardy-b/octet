'use strict';
const $=id=>document.getElementById(id);
let state={}, volumeBusy=false, requestSerial=0, albumTracks=[];
let activePlaylist=null,playlistRows=[],playlistOffset=0,playlistHasMore=false,playlistsBusy=false,playlistPlayBusy=false,playlistLoadSerial=0;
const time=seconds=>{const n=Math.max(0,Math.floor(Number(seconds)||0));return Math.floor(n/60)+':'+String(n%60).padStart(2,'0')};
function notice(message='',error=false){$('notice').textContent=message;$('notice').classList.toggle('error',error)}
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw Error(result.error||'Could not complete that action');return result}
function render(next){state=next;renderLevelControls(next);$('dsp-profile-name').textContent=next.profile;$('dsp-profile-description').textContent=next.profile_description;$('connection').textContent=next.ready?'● Octet online':'● DSP unavailable';$('connection').classList.toggle('online',next.ready);$('playback-label').textContent={play:'Playing',pause:'Paused',stop:'Stopped'}[next.playback]||'Stopped';const current=next.current;$('song-title').textContent=current?.title||'Room to listen.';$('song-artist').textContent=current?current.artist:'Choose a track to begin.';if(current?.cover){if($('cover').getAttribute('src')!==current.cover)$('cover').src=current.cover;$('cover').hidden=false;$('cover-placeholder').hidden=true}else{$('cover').hidden=true;$('cover-placeholder').hidden=false}$('toggle').textContent=next.playback==='play'?'Ⅱ':'▶';$('toggle').setAttribute('aria-label',next.playback==='play'?'Pause':'Play');$('mute').textContent=next.muted?'Unmute':'Mute';$('mute').setAttribute('aria-pressed',String(next.muted));if(!volumeBusy){$('volume').value=next.volume;$('volume-value').textContent=Math.round(next.volume)+' dB'}if(document.activeElement!==$('seek')){$('seek').max=Math.max(1,next.duration);$('seek').value=next.elapsed}$('seek').disabled=!next.duration;$('elapsed').textContent=time(next.elapsed);$('duration').textContent=time(next.duration);renderQueue(next.queue||[]);if(next.player_error)notice(next.player_error,true)}
async function control(name,data={}){try{render(await api('/api/control/'+name,data));notice()}catch(e){notice(e.message,true)}}
function renderQueue(rows){$('queue-count').textContent=rows.length;const queue=$('queue');queue.replaceChildren();if(!rows.length){const p=document.createElement('p');p.className='empty';p.textContent='Your queue is clear.';queue.append(p);return}for(const row of rows){const div=document.createElement('div');div.className='queue-track'+(row.position===state.song_position?' current':'');const number=document.createElement('span');number.className='number';number.textContent=row.position===state.song_position&&state.playback==='play'?'♫':row.position+1;const button=document.createElement('button');button.textContent=row.title;button.title=row.title+' · '+row.artist;button.onclick=()=>control('position',{position:row.position});div.append(number,button);queue.append(div)}}
function renderTracks(rows,playlistIdentifier=null){const root=$('tracks');root.replaceChildren();if(!rows.length){const p=document.createElement('p');p.className='empty';p.textContent=playlistIdentifier?'This playlist has no available tracks.':'No tracks here yet. Search for something you love.';root.append(p);return}for(const [index,row] of rows.entries()){const div=document.createElement('div');div.className='track';const art=row.cover?document.createElement('img'):document.createElement('div');art.className='small-art';if(row.cover){art.src=row.cover;art.alt='';art.loading='lazy'}const info=document.createElement('div');info.className='track-info';const title=document.createElement('button');title.className='track-title';title.textContent=row.title;title.title='Play '+row.title;title.onclick=()=>playlistIdentifier?playPlaylist(playlistIdentifier,index,row.id):control('play',{id:row.id});const detail=document.createElement('p');detail.textContent=row.artist+' · '+row.album;info.append(title,detail);const duration=document.createElement('span');duration.className='track-time';duration.textContent=time(row.duration);const add=document.createElement('button');add.className='add';add.textContent='+';add.setAttribute('aria-label','Add '+row.title+' to queue');add.onclick=()=>control('enqueue',{id:row.id});div.append(art,info,duration,add);root.append(div)}}
async function loadTracks(query=null){const serial=++requestSerial;albumTracks=[];$('play-album').hidden=true;notice(query?'Searching Tidal…':'Loading favorites…');libraryView(query===null?'favorites':'search');try{const data=await api(query===null?'/api/favorites':'/api/search?q='+encodeURIComponent(query));if(serial!==requestSerial)return;renderTracks(data.tracks);const albums=data.albums||[];const fragment=document.createDocumentFragment();for(const album of albums){const div=document.createElement('div');div.className='track';const icon=document.createElement('div');icon.className='small-art';icon.textContent='◉';icon.style.display='grid';icon.style.placeItems='center';const info=document.createElement('div');info.className='track-info';const title=document.createElement('button');title.className='track-title';title.textContent=album.title;title.onclick=()=>loadAlbum(album.id,album.title);const detail=document.createElement('p');detail.textContent=album.artist+' · Album · '+album.tracks+' tracks';info.append(title,detail);const open=document.createElement('button');open.className='add';open.textContent='›';open.setAttribute('aria-label','Open album '+album.title);open.onclick=()=>loadAlbum(album.id,album.title);div.append(icon,info,open);fragment.append(div)}if(albums.length&&!data.tracks.length)$('tracks').replaceChildren(fragment);else $('tracks').prepend(fragment);$('result-label').textContent=query===null?data.tracks.length+' tracks':(data.tracks.length+albums.length)+' results';notice()}catch(e){if(serial!==requestSerial)return;renderTracks([]);notice(e.message,true)}}
async function loadAlbum(id,title){const serial=++requestSerial;libraryView('album');albumTracks=[];$('play-album').hidden=true;notice('Loading '+title+'…');try{const data=await api('/api/album?id='+encodeURIComponent(id));if(serial!==requestSerial)return;albumTracks=data.tracks;renderTracks(albumTracks);$('play-album').hidden=!albumTracks.length;$('result-label').textContent=albumTracks.length+' tracks';notice(title)}catch(e){notice(e.message,true)}}
function libraryView(view){
  for(const [id,selected] of [['favorites',view==='favorites'],['playlists',view==='playlists'||view==='playlist']]){
    $(id).classList.toggle('active',selected);$(id).setAttribute('aria-pressed',String(selected));
  }
  $('playlist-header').hidden=view!=='playlist';$('more-playlists').hidden=true;
  if(view!=='playlist')activePlaylist=null;
  if(view!=='album'){$('play-album').hidden=true;albumTracks=[];}
}
function playlistBusy(value){
  playlistPlayBusy=value;
  for(const button of document.querySelectorAll('.playlist-play'))button.disabled=value||button.dataset.empty==='true';
}
function renderPlaylists(){
  const root=$('tracks');root.replaceChildren();
  if(!playlistRows.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='No saved playlists found in your Tidal account.';root.append(empty);}
  for(const playlist of playlistRows){
    const row=document.createElement('div');row.className='track playlist-row';
    const art=playlist.cover?document.createElement('img'):document.createElement('div');art.className='small-art';
    if(playlist.cover){art.src=playlist.cover;art.alt='';art.loading='lazy';}else art.textContent='≡';
    const info=document.createElement('div');info.className='track-info';
    const title=document.createElement('button');title.className='track-title';title.textContent=playlist.title;title.setAttribute('aria-label','Open playlist '+playlist.title);title.onclick=()=>loadPlaylist(playlist.id);
    const meta=document.createElement('p');meta.textContent=playlist.creator+' · '+playlist.tracks+' tracks';info.append(title,meta);
    const play=document.createElement('button');play.className='quiet-button playlist-play';play.textContent='Play all ▶';play.setAttribute('aria-label','Play all of '+playlist.title);play.dataset.empty=String(!playlist.tracks);play.disabled=playlistPlayBusy||!playlist.tracks;play.onclick=()=>playPlaylist(playlist.id);
    row.append(art,info,play);root.append(row);
  }
  $('result-label').textContent=playlistRows.length+' playlists';$('more-playlists').hidden=!playlistHasMore;$('more-playlists').disabled=playlistsBusy;
}
async function loadPlaylists(reset=true){
  if(playlistsBusy&&!reset)return;
  const serial=++requestSerial;playlistLoadSerial=serial;libraryView('playlists');playlistsBusy=true;
  if(reset){playlistRows=[];playlistOffset=0;playlistHasMore=false;$('tracks').replaceChildren();}
  $('more-playlists').disabled=true;notice('Loading Tidal playlists…');
  try{
    const data=await api('/api/playlists?offset='+playlistOffset);if(serial!==requestSerial)return;
    const known=new Set(playlistRows.map(row=>row.id));for(const row of data.playlists){if(!known.has(row.id)){playlistRows.push(row);known.add(row.id);}}
    playlistOffset=data.next_offset;playlistHasMore=data.has_more;renderPlaylists();notice();
  }catch(error){if(serial===requestSerial)notice(error.message,true);}
  finally{if(serial===playlistLoadSerial)playlistsBusy=false;if(serial===requestSerial)$('more-playlists').disabled=false;}
}
async function loadPlaylist(id){
  const serial=++requestSerial;libraryView('playlist');activePlaylist=null;
  $('playlist-title').textContent='Loading playlist…';$('playlist-meta').textContent='';$('play-playlist').disabled=true;$('play-playlist').dataset.empty='true';$('tracks').replaceChildren();notice('Loading the complete playlist…');
  try{
    const data=await api('/api/playlist?id='+encodeURIComponent(id)+'&refresh=1');if(serial!==requestSerial)return;
    activePlaylist=data;$('playlist-title').textContent=data.playlist.title;
    $('playlist-meta').textContent=data.tracks.length+' playable tracks'+(data.unavailable?' · '+data.unavailable+' unavailable omitted':'')+' · continues in playlist order';
    $('play-playlist').dataset.empty=String(!data.tracks.length);$('play-playlist').disabled=playlistPlayBusy||!data.tracks.length;
    $('result-label').textContent=data.tracks.length+' tracks';renderTracks(data.tracks,id);notice();
  }catch(error){if(serial===requestSerial){$('playlist-title').textContent='Playlist unavailable';notice(error.message,true);}}
}
async function playPlaylist(id,position=0,track=null){
  if(playlistPlayBusy)return;playlistBusy(true);notice('Queuing the complete playlist…');
  try{
    const body={id,position};if(track!==null)body.track_id=track;
    render(await api('/api/control/play-playlist',body));notice('Playing in playlist order. The following tracks play automatically.');
  }catch(error){notice(error.message,true);}finally{playlistBusy(false);}
}
$('playlists').onclick=()=>loadPlaylists();
$('more-playlists').onclick=()=>loadPlaylists(false);
$('playlist-back').onclick=()=>{if(playlistRows.length){++requestSerial;libraryView('playlists');renderPlaylists();notice();}else loadPlaylists();};
$('play-playlist').onclick=()=>{if(activePlaylist)playPlaylist(activePlaylist.playlist.id);};

$('play-album').onclick=async()=>{const tracks=albumTracks.slice();if(!tracks.length)return;$('play-album').disabled=true;try{await api('/api/control/clear',{});for(const track of tracks)await api('/api/control/enqueue',{id:track.id});render(await api('/api/control/position',{position:0}));notice('Album queued in order.')}catch(e){notice(e.message,true)}finally{$('play-album').disabled=false}};
$('search-form').onsubmit=e=>{e.preventDefault();const query=$('search').value.trim();if(query)loadTracks(query)};
$('favorites').onclick=()=>loadTracks();
$('toggle').onclick=()=>control(state.playback==='play'?'pause':'resume');
$('previous').onclick=()=>control('previous');$('next').onclick=()=>control('next');$('stop').onclick=()=>control('stop');$('clear').onclick=()=>control('clear');$('mute').onclick=()=>control('mute',{muted:!state.muted});
$('volume').oninput=()=>{volumeBusy=true;$('volume-value').textContent=$('volume').value+' dB'};
$('volume').onchange=async()=>{const value=Number($('volume').value);await control('volume',{db:value});volumeBusy=false;refresh()};
$('seek').onchange=()=>control('seek',{seconds:Number($('seek').value)});
async function refresh(){try{render(await api('/api/status'))}catch(e){$('connection').textContent='● Reconnecting…';$('connection').classList.remove('online')}}
refresh();loadTracks();setInterval(refresh,2500);
function renderLevelControls(next){$('volume').max=next.max_db;$('unlock-level').textContent=next.level_unlocked?'Lock standard range':'Unlock higher levels';$('unlock-level').setAttribute('aria-pressed',String(next.level_unlocked));$('level-range-note').textContent=next.level_unlocked?'Higher range unlocked: up to 0 dB. Unlocking does not change the volume.':'Standard range: −60 to −18 dB.'}
$('unlock-level').onclick=async()=>{try{render(await api('/api/control/unlock',{enabled:!state.level_unlocked}));notice(state.level_unlocked?'Higher levels unlocked. The listening level has not changed.':'Standard listening range restored.')}catch(error){notice(error.message,true)}};