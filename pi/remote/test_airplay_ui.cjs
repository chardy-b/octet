/* Lightweight DOM fixture for AirPlay state rendering; no browser dependencies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function element(){return {textContent:'',dataset:{},value:'',classList:{toggle(){}},replaceChildren(){},append(){}};}
const nodes = new Map();
const get = id => {if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
const context = vm.createContext({document:{getElementById:get,createElement:element,
  querySelector:get,querySelectorAll:()=>[]},playlistPlayBusy:false});
const code = fs.readFileSync(path.join(__dirname,'app.js'),'utf8').split("$('source-choice').onchange")[0];
vm.runInContext(code + '\nfunction renderPlayer(next){state=next;} function notice(){}',context);
const base = {source:'airplay',source_label:'AirPlay',ready:true,muted:true,source_switching_available:true,
  sources:[{id:'airplay',label:'AirPlay',available:true,group:'Music'}]};
function render(airplay){context.next={...base,airplay};vm.runInContext('render(next)',context);}
render({active:false});
assert.match(get('song-artist').textContent,/Waiting/);
assert.match(get('source-status').textContent,/waiting/);
assert.equal(get('.transport').hidden,true);
render({preparing:true});
assert.match(get('source-status').textContent,/Preparing.*muted/);
render({active:true});
assert.match(get('source-status').textContent,/cast active/);
render({error:'AirPlay capture failed'});
assert.equal(get('source-status').textContent,'AirPlay capture failed');
console.log('AirPlay UI: waiting, preparing, active, failure and hidden Tidal controls passed.');
