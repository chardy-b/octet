'use strict';
// The local preview analyzes the same decoded MP3 that reaches the speakers.
// No microphone, synthetic beat clock, or second copy of the track is used.
const OctetSimulation = (() => {
  const tracks = {
    edm: {title:'EDM Detection Mode', file:'edm.mp3'},
    classical: {title:'Gymnopédie No. 1 · Erik Satie', file:'classical.mp3'},
  };
  const limit = value => Math.max(0,Math.min(1,value));
  const level = db => db <= -65 ? 0 : Math.pow(limit((db+65)/60),1.65);
  const quiet = () => ({energy:0,peak:0,bands:[0,0,0,0]});

  function create(audio,onState,onError) {
    let context=null,analyser=null,wave=null,frequencies=null,selected='edm';
    function prepare() {
      if(context)return;
      const Audio = window.AudioContext || window.webkitAudioContext;
      if(!Audio)throw Error('This browser cannot analyze sample audio.');
      context=new Audio({latencyHint:'interactive'});
      const source=context.createMediaElementSource(audio);
      analyser=context.createAnalyser();analyser.fftSize=2048;
      analyser.smoothingTimeConstant=.72;
      source.connect(analyser);analyser.connect(context.destination);
      wave=new Float32Array(analyser.fftSize);
      frequencies=new Float32Array(analyser.frequencyBinCount);
    }
    async function play() {
      prepare();await context.resume();await audio.play();
    }
    function select(name) {
      if(!Object.hasOwn(tracks,name))return Promise.resolve();
      const playing=!audio.paused;
      audio.pause();selected=name;
      audio.src='/visualizer-media/'+tracks[name].file;
      audio.load();
      return playing ? play() : Promise.resolve();
    }
    audio.volume=.35;
    audio.addEventListener('play',()=>{
      try{
        prepare();context.resume().catch(error=>onError(error.message));
        onState('playing');
      }catch(error){audio.pause();onError(error.message);}
    });
    audio.addEventListener('pause',()=>onState('paused'));
    audio.addEventListener('waiting',()=>onState('loading'));
    audio.addEventListener('playing',()=>onState('playing'));
    audio.addEventListener('error',()=>onError('Could not load the selected sample MP3.'));
    function sample() {
      if(!analyser||audio.paused||audio.readyState<2||context.state!=='running')return quiet();
      analyser.getFloatTimeDomainData(wave);
      analyser.getFloatFrequencyData(frequencies);
      let sum=0,maximum=0;
      for(const value of wave){sum+=value*value;maximum=Math.max(maximum,Math.abs(value));}
      const rms=Math.sqrt(sum/wave.length);
      const powers=[0,0,0],counts=[0,0,0];let weighted=0,total=0;
      for(let i=1;i<frequencies.length;i++){
        const frequency=i*context.sampleRate/analyser.fftSize;
        if(frequency<30||frequency>14000)continue;
        const power=Number.isFinite(frequencies[i])?10**(frequencies[i]/10):0;
        const band=frequency<200?0:frequency<3000?1:2;
        powers[band]+=power;counts[band]++;
        total+=power;weighted+=frequency*power;
      }
      const bands=powers.map((power,i)=>power>0?limit((10*Math.log10(power/Math.max(1,counts[i]))+80)/65):0);
      const centroid=total>1e-12?limit(Math.log2(Math.max(100,weighted/total)/180)/5.4):0;
      return {energy:level(20*Math.log10(Math.max(1e-9,rms))),
        peak:level(20*Math.log10(Math.max(1e-9,maximum))),bands:[...bands,centroid]};
    }
    return {play,select,sample,pause:()=>audio.pause(),track:()=>selected};
  }
  return {tracks,create};
})();
