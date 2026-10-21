const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const videos=new Map(),imageSources=[];let uploads=0,draws=0,solarLight,solarView,videoZoom,compilations=0;
for(const name of ['sky','water','stars','sun','lunar']){
  const listeners=new Map();
  videos.set(name+'-video',{id:name+'-video',dataset:{src:'/visualizer-media/'+name+'.mp4'},paused:true,readyState:0,currentTime:0,duration:name==='sun'?18:90,seeking:false,
    addEventListener:(event,fn)=>listeners.set(event,fn),getAttribute(name){return this[name]||null;},
    load(){this.readyState=4;this.playbackRate=this.defaultPlaybackRate;listeners.get('loadeddata')?.();},
    play(){this.paused=false;return Promise.resolve();},pause(){this.paused=true;},
    requestVideoFrameCallback(fn){this.nextFrame=fn;},
    advance(){this.currentTime+=1/24;this.nextFrame?.();},
    finishSeek(){this.seeking=false;listeners.get('seeked')?.();},
  });
}
const gl={
  createShader:()=>({}),createProgram:()=>({}),createBuffer:()=>({}),createTexture:()=>({}),
  getShaderParameter:()=>true,getProgramParameter:()=>true,getAttribLocation:()=>0,
  getUniformLocation:(_,name)=>name,isContextLost:()=>false,
  texImage2D(...args){if(args.at(-1)?.id?.endsWith('-video'))uploads++;},
  drawArrays(){draws++;},
};
for(const method of ['shaderSource','compileShader','attachShader','linkProgram','deleteShader','useProgram','bindBuffer','bufferData','enableVertexAttribArray','vertexAttribPointer','activeTexture','bindTexture','pixelStorei','texParameteri','uniform1i','uniform1f','uniform2f','uniform3f','uniform4f','viewport'])gl[method]=()=>{};
gl.uniform4f=(name,...values)=>{if(name==='eclipseLight')solarLight=values;};
gl.uniform2f=(name,...values)=>{if(name==='eclipseView')solarView=values;};
gl.uniform1f=(name,value)=>{if(name==='videoZoom')videoZoom=value;};
gl.compileShader=()=>compilations++;
const listeners=new Map();
const canvas={getContext:()=>gl,addEventListener:(name,fn)=>listeners.set(name,fn)};
const document={hidden:false,getElementById:id=>videos.get(id)};
const context=vm.createContext({document,canvas,devicePixelRatio:1,Image:class{
  set src(value){imageSources.push(value);this.complete=true;this.naturalWidth=1;this.onload?.();}
}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'visualizer-solar.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'visualizer-themes.js'),'utf8'),context);
const renderer=vm.runInContext('OctetThemes.create(canvas)',context);
assert.equal(imageSources.length,0);assert.equal(compilations,2);
renderer.resize(1280,720);
assert.equal(canvas.width,1280);assert.equal(canvas.height,720);
renderer.resize(3840,2160);
assert.equal(canvas.width,3840);assert.equal(canvas.height,2160);
context.devicePixelRatio=2;renderer.resize(1920,1080);
assert.equal(canvas.width,3840);assert.equal(canvas.height,2160);
renderer.resize(7680,4320);
assert.ok(canvas.width*canvas.height<=16777216+8192);
assert.ok(canvas.width<=8192&&canvas.height<=8192);
context.devicePixelRatio=1;renderer.resize(1280,720);
renderer.setActive('turell 2');
assert.deepEqual(imageSources,['/visualizer-media/sky.jpg']);
assert.equal(videos.get('sky-video').paused,false);assert.equal(videos.get('water-video').paused,true);
assert.equal(videos.get('sky-video').playbackRate,1);
renderer.render('turell 2',1,.5,.6);const initial=uploads;
renderer.render('turell 2',2,.5,.6);assert.equal(uploads,initial);
videos.get('sky-video').advance();renderer.render('turell 2',3,.5,.6);assert.equal(uploads,initial+1);
renderer.setActive('turell 3');assert.equal(videos.get('sky-video').paused,true);assert.equal(videos.get('stars-video').paused,false);
renderer.setActive('turell 4');assert.ok([...videos.values()].every(video=>video.paused));
renderer.setActive('turell 5');assert.equal(videos.get('sun-video').paused,false);assert.equal(videos.get('stars-video').paused,true);
renderer.render('turell 5',3.5,.5,.6);
assert.equal(compilations,4); // Solar compiles separately, once.
assert.equal(videoZoom,1);
renderer.setMediaZoom(2);renderer.render('turell 5',3.5,.5,.6);
assert.equal(videoZoom,2);assert.equal(renderer.mediaState().zoom,2);
renderer.setMediaZoom(10);assert.equal(renderer.mediaState().zoom,3);
renderer.setMediaZoom(.1);assert.equal(renderer.mediaState().zoom,.5);
renderer.setMediaZoom(NaN);assert.equal(renderer.mediaState().zoom,.5);
renderer.setMediaZoom(1);
// The modeled eclipse clock is independent of the real Sun texture's cadence.
const sun=videos.get('sun-video');
renderer.seekMedia(360);renderer.render('turell 5',4,.5,.6);
assert.equal(renderer.mediaState().time,360);assert.equal(renderer.mediaState().duration,720);
assert.equal(renderer.mediaState().phase,'Totality');assert.equal(solarLight[1],1);
assert.deepEqual(solarView,[0,1]); // No photosphere light; Realism dims the field.
renderer.seekMedia(180);renderer.render('turell 5',4,.5,.6);
assert.ok(solarView[0]>0&&solarView[0]<1);assert.equal(solarView[1],1);
renderer.seekMedia(360);
assert.equal(sun.currentTime,0); // Eclipse scrubbing does not seek the texture movie.
renderer.toggleMedia();assert.equal(sun.paused,true);
const held=renderer.mediaState().time;renderer.advance(1);assert.equal(renderer.mediaState().time,held);
const heldMotion=solarLight[0];renderer.render('turell 5',4,.5,.6);assert.equal(solarLight[0],heldMotion);
renderer.resumeMedia();assert.equal(sun.paused,true);
renderer.toggleMedia();assert.equal(sun.paused,false);
assert.equal(renderer.mediaState().mode,'realism');
renderer.setSolarOptions({speed:2,tone:'cycle',mode:'artistic'});assert.equal(renderer.mediaState().mode,'artistic');renderer.advance(1);
assert.equal(renderer.mediaState().time,362);assert.equal(renderer.mediaState().tone,'cycle');
renderer.render('turell 5',4,.5,.6);assert.ok(solarLight[3]>=0&&solarLight[3]<=1);assert.deepEqual(solarView,[0,0]);
assert.equal(solarLight[0],heldMotion+1); // Corona motion keeps its gentle pace at 2× eclipse speed.
renderer.setActive('turell 5',false,true);const reducedTime=renderer.mediaState().time;
renderer.advance(1);assert.equal(renderer.mediaState().time,reducedTime);assert.equal(sun.paused,true);
renderer.render('turell 5',4,.5,.6);assert.equal(solarLight[0],heldMotion+1);
renderer.setActive('turell 5');renderer.seekMedia(-50);assert.equal(renderer.mediaState().time,0);
renderer.seekMedia(Infinity);assert.equal(renderer.mediaState().time,0);
renderer.seekMedia(10000);assert.ok(renderer.mediaState().time<720&&renderer.mediaState().time>719);
renderer.setActive('turell 2');renderer.seekMedia(30);assert.equal(videos.get('sky-video').currentTime,30);
renderer.render('turell 2',5,.5,.6);renderer.setActive('turell 5');renderer.render('turell 5',5,.5,.6);
assert.equal(compilations,4);renderer.setActive('turell 2');
renderer.setMediaZoom(1.5);
renderer.setActive('turell 3');renderer.seekMedia(45);assert.equal(videos.get('stars-video').currentTime,45);
assert.equal(renderer.mediaState().zoom,1);renderer.setActive('turell 2');assert.equal(renderer.mediaState().zoom,1.5);
renderer.setActive('turell 3');
renderer.setActive('overlap');assert.equal(renderer.mediaState(),null);renderer.seekMedia(20);
assert.equal(videos.get('stars-video').currentTime,45);
renderer.setActive('turell lunar');assert.equal(videos.get('lunar-video').paused,false);assert.equal(videos.get('sun-video').paused,true);
renderer.render('turell lunar',4,.5,.6);
renderer.setActive('water');assert.equal(videos.get('lunar-video').paused,true);assert.equal(videos.get('water-video').paused,false);assert.equal(videos.get('stars-video').paused,true);
assert.equal(videos.get('sun-video').paused,true);
renderer.setActive('well');assert.equal(videos.get('sky-video').paused,true);assert.equal(videos.get('stars-video').paused,false);
renderer.setActive('overlap');assert.ok(['sky','water','stars'].every(name=>!videos.get(name+'-video').paused));assert.equal(videos.get('sun-video').paused,true);
renderer.setActive('overlap',true);assert.ok([...videos.values()].every(video=>video.paused));
renderer.setActive('overlap',false,true);assert.ok([...videos.values()].every(video=>video.paused));
renderer.setActive('turell 1');assert.ok([...videos.values()].every(video=>video.paused));
renderer.setActive('turell 2');listeners.get('webglcontextlost')({preventDefault(){}});
assert.ok([...videos.values()].every(video=>video.paused));
const count=draws;renderer.render('turell 2',4,.5,.5);assert.equal(draws,count);
listeners.get('webglcontextrestored')();assert.equal(videos.get('sky-video').paused,false);
console.log('Video checks passed: native cadence/4K, decoded-frame uploads, hybrid clock/seek/speed/color, independent corona animation, bounded seeks, persistent pause/resume, theme/visibility/reduced-motion pause, and context recovery.');
