const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const events=new Map();let sources=0,resumes=0,bin=6;
const audio={paused:true,readyState:4,volume:0,addEventListener:(name,fn)=>events.set(name,fn),
  pause(){if(!this.paused){this.paused=true;events.get('pause')?.();}},
  async play(){this.paused=false;events.get('play')?.();events.get('playing')?.();},load(){}};
const analyser={fftSize:2048,frequencyBinCount:1024,connect(){},
  getFloatTimeDomainData(data){for(let i=0;i<data.length;i++)data[i]=Math.sin(i*.15)*.3;},
  getFloatFrequencyData(data){data.fill(-100);data[bin]=-15;}};
class AudioContext{
  constructor(){this.sampleRate=48000;this.state='running';this.destination={};}
  createMediaElementSource(){sources++;return{connect(){}};}
  createAnalyser(){return analyser;}
  async resume(){resumes++;}
}
const states=[],errors=[];
const context=vm.createContext({console,window:{AudioContext},audio,states,errors});
vm.runInContext(fs.readFileSync(path.join(__dirname,'visualizer-audio.js'),'utf8'),context);
const simulation=vm.runInContext('OctetSimulation.create(audio,state=>states.push(state),message=>errors.push(message))',context);
(async()=>{
  assert.equal(simulation.sample().energy,0);
  await simulation.select('edm');assert.match(audio.src,/edm\.mp3$/);
  await simulation.play();
  const bass=simulation.sample();assert.ok(bass.energy>.3);assert.ok(bass.bands[0]>bass.bands[2]);
  bin=250;const treble=simulation.sample();assert.ok(treble.bands[2]>treble.bands[0]);assert.ok(treble.bands[3]>bass.bands[3]);
  await simulation.select('classical');assert.match(audio.src,/classical\.mp3$/);assert.equal(audio.paused,false);
  assert.equal(sources,1);assert.ok(resumes>0);
  simulation.pause();assert.equal(simulation.sample().energy,0);
  const selected=audio.src;await simulation.select('../outside');assert.equal(audio.src,selected);
  assert.equal(errors.length,0);assert.ok(states.includes('playing'));assert.ok(states.includes('paused'));
  console.log('Sample audio checks passed: real analyzer graph, audible playback, bass/treble discrimination, centroid, track switching, pause and invalid selections.');
})().catch(error=>{console.error(error);process.exitCode=1;});
