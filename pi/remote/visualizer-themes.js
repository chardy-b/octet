'use strict';
// Geometric light studies. Analytic ray intersections give the solids
// depth without a library, a moving camera, or a costly ray-marching loop.
const OctetThemes = (() => {
  const descriptions = {
    'turell 1': ['turell 1', 'One field. One halo. Your music.'],
    'turell 2': ['turell 2 · Sky', 'Moving daylight inside a fixed circle of light.'],
    'turell 3': ['turell 3 · Night', 'A moving night sky, framed by colored light.'],
    'turell 4': ['turell 4 · Eclipse', 'A black disc. A quiet, luminous corona.'],
    'turell 5': ['turell 5 · Solar', 'Real solar detail. A smooth passing Moon. A living corona.'],
    'turell lunar': ['turell · Lunar', 'The changing phases of the Moon, held in a fixed circle of light.'],
    well: ['Well', 'A square opening into another night.'],
    overlap: ['Overlap', 'Daylight, water, and night in the same space.'],
    water: ['Water · Underwater', 'Moving water, refracted light, and a room beneath the surface.'],
  };
  const seeds = [
    'LHQieImkr1PsHRvoGiq5mFA1UV1PrWHT1wD661qeQfwyM8ZlkPgyYCZNhGVz9IHU',
    'Cwss76355PFc3RHtD3I8pqMX7x1w88kbPN5KN3UhHAFgUOeJoVvsg8IU6owpccvG',
    'KZbVMlwOGNADAlaYtrga6xxY75ctqV4GrI8FMuoYyxWIHpGs4m53SKEzQ1S1oH5j',
    'KN1VWCCXacyuHvSaGMmWXiADL6JoiX5CUwKKOqNoQNjKXnCEZHN7PY8Y9JV5X7sO',
    '0j1jUQ5xfIsaMMT0Xj6VEtuHEChjpt5VY6zVvYyaoTWvmEUgjc10QJdB9u4GLca7',
  ];
  const themeIds={well:3,overlap:5,'turell 2':6,'turell 3':7,'turell 4':8,water:9,'turell 5':10,'turell lunar':11};
  const vertex = `attribute vec2 position; void main(){gl_Position=vec4(position,0.0,1.0);}`;
  const fragment = `
precision highp float;
uniform vec2 resolution;
uniform float time, energy, peak, seed;
uniform vec4 spectrum;
uniform vec3 fieldColor, haloColor, edgeColor;
uniform float edgeLight, videoZoom, circleSize;
uniform int theme;
uniform sampler2D sky, water, stars, sun, lunar;
float sat(float x){return clamp(x,0.0,1.0);}
mat2 turn(float a){float c=cos(a),s=sin(a);return mat2(c,-s,s,c);}
vec3 spinY(vec3 p,float a){p.xz=turn(a)*p.xz;return p;}
vec3 spinX(vec3 p,float a){p.yz=turn(a)*p.yz;return p;}
vec2 drift(vec2 p){return clamp(p+vec2(sin(time*.025),cos(time*.018))*.035,.01,.99);}
vec3 cloud(vec2 uv){return texture2D(sky,clamp(uv*.94+.03,.01,.99)).rgb;}
vec3 liquid(vec2 uv){
  uv=uv*.72+.14;
  uv+=vec2(sin(uv.y*9.0+time*.15),cos(uv.x*7.0+time*.12))*.012;
  return texture2D(water,drift(uv)).rgb;
}
vec3 night(vec2 uv){
  vec3 c=texture2D(stars,clamp(uv,.01,.99)).rgb;
  return c*(.70+energy*.24);
}
float boxHit(vec3 ro,vec3 rd,vec3 center,vec3 size,out vec3 normal){
  vec3 q=ro-center;
  vec3 safe=sign(rd+vec3(.000001))*max(abs(rd),vec3(.000001));
  vec3 a=(-size-q)/safe,b=(size-q)/safe;
  vec3 lo=min(a,b),hi=max(a,b);
  float near=max(max(lo.x,lo.y),lo.z),far=min(min(hi.x,hi.y),hi.z);
  if(near>far||far<0.0)return 1000.0;
  normal=-sign(rd)*step(lo.yzx,lo.xyz)*step(lo.zxy,lo.xyz);
  vec3 bevel=max(abs(q+rd*max(near,0.0))-size+vec3(.008),0.0);
  if(length(bevel)>.0001)normal=sign(q+rd*max(near,0.0))*normalize(bevel);
  return max(near,0.0);
}
vec3 lit(vec3 base,vec3 n,vec3 p,float ambient){
  vec3 light=normalize(vec3(-.55+sin(time*.065)*.25,.7+cos(time*.049)*.12,1.3));
  float diffuse=max(dot(n,light),0.0);
  float fill=max(dot(n,normalize(vec3(.75,-.35,.9))),0.0);
  float sheen=pow(max(dot(reflect(-light,n),normalize(vec3(0.,0.,4.5)-p)),0.),32.);
  float edge=pow(1.-max(dot(n,normalize(vec3(0.,0.,4.5)-p)),0.),3.);
  return base*(ambient+diffuse*.64)+base*vec3(.08,.10,.14)*fill+vec3(.05,.045,.037)*sheen+vec3(.025,.035,.045)*edge;
}
vec3 well(vec3 ro,vec3 rd){
  float nearest=1000.;vec3 hitNormal=vec3(0.);vec3 hitPoint=vec3(0.);float layer=0.;
  for(int i=0;i<4;i++){
    float k=float(i),outer=1.04-k*.185,inner=outer-.17,z=-k*.22;
    for(int j=0;j<4;j++){
      vec3 center,size;
      if(j==0){center=vec3(0.,outer-.085,z);size=vec3(outer,.085,.115);}
      else if(j==1){center=vec3(0.,-outer+.085,z);size=vec3(outer,.085,.115);}
      else if(j==2){center=vec3(outer-.085,0.,z);size=vec3(.085,inner,.115);}
      else{center=vec3(-outer+.085,0.,z);size=vec3(.085,inner,.115);}
      vec3 n;float t=boxHit(ro,rd,center,size,n);
      if(t<nearest){nearest=t;hitNormal=n;hitPoint=ro+rd*t;layer=k;}
    }
  }
  if(nearest<999.){
    vec3 base=mix(vec3(.74,.77,.64),vec3(.80,.73,.61),spectrum.w*.25);
    float depth=1.-layer*.095;
    float warm=spectrum.x*.04;
    float edge=max(abs(hitPoint.x),abs(hitPoint.y));
    float recess=.91+.09*smoothstep(.0,.12,abs(edge-(1.04-layer*.185)));
    return lit(base+vec3(warm,warm*.5,0.),hitNormal,hitPoint,.27)*depth*recess;
  }
  float t=(-1.2-ro.z)/rd.z;vec2 p=(ro+rd*t).xy;
  if(max(abs(p.x),abs(p.y))<.35)return night(p/.70+.5)*.88;
  return vec3(0.);
}
vec3 overlap(vec2 p){
  vec3 result=vec3(0.);
  float aa=2.0/min(resolution.x,resolution.y);
  for(int i=0;i<3;i++){
    vec2 center=i==0?vec2(0.,.28):i==1?vec2(-.30,-.20):vec2(.30,-.20);
    vec2 q=p-center;float r=length(q);
    float mask=1.-smoothstep(.58-aa,.58+aa,r);
    vec2 uv=q/1.16+.5;
    vec3 base=i==0?vec3(.48,.69,.78):i==1?vec3(.85,.64,.49):vec3(.62,.59,.80);
    vec3 nature=i==0?cloud(uv):i==1?liquid(uv):night(uv)*1.6;
    base=mix(base,nature,i==0?.42:i==1?.20:.25);
    float normalZ=sqrt(max(0.,1.-r*r/(.58*.58)));
    float shading=.86+.14*normalZ;
    float opacity=.82+spectrum[i]*.035+sin(time*.055+float(i)*2.09)*.035;
    vec3 light=base*mask*shading*opacity;
    light+=vec3(.05,.055,.065)*pow(1.-normalZ,5.)*mask;
    result=1.-(1.-result)*(1.-light);
  }return result;
}
vec3 skyCircle(vec2 p,int source){
  float r=length(p),aperture=.28;
  if(r>1.)return vec3(0.);
  float distanceFromEdge=max(r-aperture,0.);
  vec3 field=fieldColor*(.81+.24*exp(-dot(p-vec2(0.,-.23),p-vec2(0.,-.23))/.75));
  float halo=exp(-pow(distanceFromEdge/(.12+energy*.012),2.))*.83;
  field=mix(field,haloColor,halo);
  float edgeWash=edgeLight*(.20*exp(-pow((1.-r)/.18,2.))+.56*exp(-pow((1.-r)/.045,2.)));
  field=mix(field,edgeColor,edgeWash);
  float lip=exp(-distanceFromEdge*170.);
  field*=1.-lip*.11;
  if(source==3&&r<aperture){
    // Fixed lunar disc: source center (1920,1080), radius ~810px.
    vec2 uv=p/(2.*aperture*videoZoom)*vec2(.420,.747)+.5;
    if(any(lessThan(uv,vec2(0.)))||any(greaterThan(uv,vec2(1.))))return vec3(0.);
    return texture2D(lunar,uv).rgb;
  }
  if(r<aperture){
    bool nighttime=source==1;
    vec2 uv=p/(2.*aperture*videoZoom)+.5;
    uv.x=(uv.x-.5)*.5625+.5;
    if(any(lessThan(uv,vec2(0.)))||any(greaterThan(uv,vec2(1.))))return vec3(0.);
    vec3 view=nighttime?night(uv)*1.10:cloud(uv);
    return view;
  }
  return field;
}
vec3 eclipse(vec2 p){
  float r=length(p),disc=.34;
  if(r<disc||r>1.)return vec3(0.);
  float angle=atan(p.y,p.x),d=r-disc;
  float bend=.014*sin(angle*3.+time*.075)+.009*sin(angle*7.-time*.043);
  float a=angle+bend*(1.-exp(-d*5.));
  float streamer=1.+.26*cos(a*2.+.18*sin(time*.025))+.09*sin(a*5.-time*.035);
  float width=(.12+energy*.012)*streamer;
  float tight=exp(-d/.018);
  float corona=exp(-pow(d/max(width,.01),.72));
  float atmosphere=exp(-d/(.29*streamer))*.22;
  float filament=pow(.5+.5*sin(a*17.+sin(a*3.)*.4+.22*sin(time*.11-d*6.)),4.)*.07*exp(-d/.24);
  float breath=1.+.035*sin(time*.16)+.018*sin(time*.071+1.3);
  float flow=1.+.045*sin(d*48.-time*.22+sin(a*11.));
  vec3 warmWhite=mix(vec3(1.,.89,.71),vec3(.90,.95,1.),spectrum.z*.18);
  return warmWhite*(tight*.65+corona+atmosphere+filament)*breath*flow*(1.+peak*.05)*(1.-smoothstep(.85,1.,r));
}
vec3 underwater(vec2 p){
  float r=length(p);
  if(r>1.)return vec3(0.);
  vec2 uv=p*.5+.5;uv.x=(uv.x-.5)*.5625+.5;
  vec2 bend=vec2(sin(p.y*6.+time*.20),cos(p.x*5.-time*.17))*.004;
  vec3 film=texture2D(water,clamp(uv+bend,.01,.99)).rgb;
  // Refract all channels together, retaining fine water detail without color blur.
  film*=vec3(.78,1.02,1.11);
  float luminance=dot(film,vec3(.21,.72,.07));
  float caustic=smoothstep(.43,.90,luminance);
  float sun=pow(.5+.5*sin(p.x*3.8+p.y*.8+time*.07),8.)*(.035+spectrum.z*.025);
  float depth=.94-.10*smoothstep(.35,1.,r)+energy*.035;
  return film*depth+vec3(.38,.73,.80)*(caustic*.10+sun);
}
vec3 scene(vec2 p){
  if(theme==6)return skyCircle(p/1.16,0);
  if(theme==7)return skyCircle(p/1.16,1);
  if(theme==11)return skyCircle(p/1.16,3);
  if(theme==8)return eclipse(p/1.16);
  if(theme==9)return underwater(p/1.16);
  vec2 parallax=vec2(sin(time*.035),cos(time*.028))*.012;
  vec3 ro=vec3(parallax,4.5),rd=normalize(vec3(p*1.13-parallax,-4.5));
  vec3 color=vec3(0.);
  if(theme==3)color=well(ro,rd);
  else if(theme==5)color=overlap(p);
  return color;
}
void main(){
  // Circular artwork spans the longer screen dimension; architecture keeps its framing.
  float span=theme>=6?max(resolution.x,resolution.y)*circleSize:min(resolution.x,resolution.y);
  float pixel=2.32/span;
  vec2 p=(gl_FragCoord.xy*2.-resolution)/span*1.16;
  vec3 color;
  float radius=length(p)/1.16;
  if(theme>=6&&radius>1.+pixel){gl_FragColor=vec4(0.,0.,0.,1.);return;}
  // Expensive surface shading runs once per pixel. Extra edge samples are
  // confined to silhouettes; Solar has analytic pixel-width antialiasing.
  bool edge=abs(radius-1.)<pixel*1.5||abs(radius-.28)<pixel*1.5||(theme==8&&abs(radius-.34)<pixel*1.5);
  if(theme==10||(theme>=6&&!edge))color=scene(p);
  else color=(scene(p+vec2(-.25,-.25)*pixel)+scene(p+vec2(.25,-.25)*pixel)+scene(p+vec2(-.25,.25)*pixel)+scene(p+vec2(.25,.25)*pixel))*.25;
  if(theme==10)color*=1.-smoothstep(1.-pixel,1.,radius);
  // Dither only lit surfaces; untouched space remains exactly black.
  if(max(max(color.r,color.g),color.b)>.001){
    float grain=fract(sin(dot(gl_FragCoord.xy,vec2(12.9898,78.233)))*43758.5453);
    color+=(grain-.5)/255.;
  }
  gl_FragColor=vec4(clamp(color,0.,1.),1.);
}`;

  // Keep the solar program separate: other themes never compile its contact
  // and corona code, and Solar does not carry the architectural renderer.
  const solarFragment=`
precision highp float;
uniform vec2 resolution;
uniform float time,energy,peak,edgeLight,videoZoom,circleSize;
uniform vec3 fieldColor,haloColor,edgeColor;
uniform vec4 spectrum;
uniform sampler2D sun;
mat2 turn(float a){float c=cos(a),s=sin(a);return mat2(c,-s,s,c);}
vec3 spinY(vec3 p,float a){p.xz=turn(a)*p.xz;return p;}
${OctetSolar.glsl}
void main(){
  float span=max(resolution.x,resolution.y)*circleSize;
  vec2 p=(gl_FragCoord.xy*2.-resolution)/span;
  float r=length(p);if(r>1.){gl_FragColor=vec4(0.,0.,0.,1.);return;}
  vec3 color=proceduralSolar(p)*(1.-smoothstep(1.-2./span,1.,r));
  if(max(max(color.r,color.g),color.b)>.001){
    float grain=fract(sin(dot(gl_FragCoord.xy,vec2(12.9898,78.233)))*43758.5453);
    color+=(grain-.5)/255.;
  }
  gl_FragColor=vec4(clamp(color,0.,1.),1.);
}`;

  function create(canvas,report=()=>{},initialTheme='turell 1',{maxPixels=16777216}={}) {
    const gl = canvas.getContext('webgl', {alpha:false, depth:false, antialias:false});
    if (!gl) return null;
    let program, uniforms, buffer, lost = false, programCache={};
    const textures = [];
    const images = [];
    const mediaNames=['sky','water','stars','sun','lunar'];
    const videos=mediaNames.map(name=>document.getElementById(name+'-video'));
    const required={well:[2],overlap:[0,1,2],'turell 2':[0],'turell 3':[2],'turell 4':[],'turell 5':[3],'turell lunar':[4],water:[1]};
    const dirty=mediaNames.map(()=>true),lastTimes=mediaNames.map(()=>-1);
    const frameTimes=mediaNames.map(()=>-1),heldVideos=new Set();
    const mediaZoom=mediaNames.map(()=>1);
    const presentedFrames=mediaNames.map(()=>0);
    const driverExtension=gl.getExtension?.('WEBGL_debug_renderer_info');
    const rendererLabel=driverExtension?String(gl.getParameter(driverExtension.UNMASKED_RENDERER_WEBGL)):'WebGL';
    const frameCallbacks=videos.map(video=>typeof video.requestVideoFrameCallback==='function');
    let activeName=initialTheme,mediaPaused=true,mediaReduced=false,activeMedia=[];
    let solarClock=0,solarMotion=0,solarPaused=false,solarZoom=1,solarSpeed=1,solarTone='natural',solarMode='realism';
    function compile(kind, source) {
      const shader = gl.createShader(kind);
      gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        const error = gl.getShaderInfoLog(shader); gl.deleteShader(shader); throw Error(error);
      }
      return shader;
    }
    function upload(index) {
      gl.activeTexture(gl.TEXTURE0 + index);
      gl.bindTexture(gl.TEXTURE_2D, textures[index]);
      if (images[index]?.complete && images[index].naturalWidth) {
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, images[index]);
      }
    }
    function uploadVideo(index){
      const video=videos[index];
      if(video.seeking||video.readyState<2||(!dirty[index]&&(frameCallbacks[index]||lastTimes[index]===video.currentTime)))return;
      const width=video.videoWidth,height=video.videoHeight;
      if(!width||!height)return;
      gl.activeTexture(gl.TEXTURE0+index);gl.bindTexture(gl.TEXTURE_2D,textures[index]);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);
      // Chromium's direct RGBA video upload is substantially faster on V3D
      // than texSubImage2D, which takes a costly copy path on this platform.
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,video);
      dirty[index]=false;lastTimes[index]=frameTimes[index]>=0?frameTimes[index]:video.currentTime;
    }
    function setActive(name,hidden=false,reduced=false){
      activeName=name;mediaPaused=hidden;mediaReduced=reduced;activeMedia=required[name]||[];
      videos.forEach((video,index)=>{
        if(hidden||!activeMedia.includes(index)||lost){video.pause();return;}
        if(!images[index]){
          const image=new Image();images[index]=image;
          image.onload=()=>{if(!lost&&video.readyState<2)upload(index);};
          image.src='/visualizer-media/'+mediaNames[index]+'.jpg';
        }
        if(!video.getAttribute('src')){video.src=video.dataset.src;video.load();}
        if(reduced||heldVideos.has(index)||(name==='turell 5'&&solarPaused)){video.pause();return;}
        if(video.paused)video.play().catch(error=>{
          if(!mediaPaused&&!mediaReduced&&activeMedia.includes(index)&&error.name!=='AbortError')report('Video paused. Tap the artwork to resume moving imagery.');
        });
      });
    }
    function activate(id){
      const key=String(id);let entry=programCache[key];
      if(!entry){
        // A fixed theme lets the Pi's GPU compiler remove unused scenes before
        // compiling their nested geometry and edge samples. A uniform branch
        // retains every scene and can stall/crash Chromium's GPU process.
        const source=id===10?solarFragment:fragment.replace('uniform int theme;',`const int theme = ${id};`);
        const vs=compile(gl.VERTEX_SHADER,vertex),fs=compile(gl.FRAGMENT_SHADER,source);
        const compiled=gl.createProgram();gl.attachShader(compiled,vs);gl.attachShader(compiled,fs);
        gl.bindAttribLocation?.(compiled,0,'position');gl.linkProgram(compiled);gl.deleteShader(vs);gl.deleteShader(fs);
        if(!gl.getProgramParameter(compiled,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(compiled));
        const names=['resolution','time','energy','peak','seed','theme','spectrum','eclipseMoon','eclipseLight','eclipseView','sunFrame','fieldColor','haloColor','edgeColor','edgeLight','videoZoom','circleSize',...mediaNames];
        entry=programCache[key]={program:compiled,uniforms:Object.fromEntries(names.map(name=>[name,gl.getUniformLocation(compiled,name)])),position:gl.getAttribLocation(compiled,'position')};
      }
      if(program===entry.program)return;
      program=entry.program;uniforms=entry.uniforms;gl.useProgram(program);
      gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.enableVertexAttribArray(entry.position);gl.vertexAttribPointer(entry.position,2,gl.FLOAT,false,0,0);
      mediaNames.forEach((name,index)=>gl.uniform1i(uniforms[name],index));
    }
    function initialize() {
      programCache={};program=null;
      buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
      gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);
      activate(themeIds[activeName]||6);
      for (const [index,name] of mediaNames.entries()) {
        textures[index]=gl.createTexture(); gl.activeTexture(gl.TEXTURE0+index); gl.bindTexture(gl.TEXTURE_2D,textures[index]);
        gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array(index===2?[10,15,25,255]:[120,150,170,255]));
        gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
        gl.uniform1i(uniforms[name],index);
        upload(index);
      }
      lost=false;
      dirty.fill(true);lastTimes.fill(-1);
    }
    initialize();
    videos.forEach((video,index)=>{
      // Play at the source cadence, including the water video's native 60 fps.
      video.muted=true;video.defaultMuted=true;video.defaultPlaybackRate=1;video.playbackRate=1;
      if(frameCallbacks[index]){
        const frame=(_now,metadata)=>{presentedFrames[index]++;dirty[index]=true;frameTimes[index]=metadata?.mediaTime??video.currentTime;video.requestVideoFrameCallback(frame);};
        video.requestVideoFrameCallback(frame);
      }
      video.addEventListener('loadeddata',()=>{dirty[index]=true;});
      video.addEventListener('seeked',()=>{
        dirty[index]=true;frameTimes[index]=video.currentTime;
        if(heldVideos.has(index)||mediaPaused||mediaReduced||!activeMedia.includes(index))video.pause();
      });
      video.addEventListener('error',()=>report('A nature video could not load. Showing its still reference frame.'));
    });
    canvas.addEventListener('webglcontextlost',event=>{event.preventDefault();lost=true;videos.forEach(video=>video.pause());});
    canvas.addEventListener('webglcontextrestored',()=>{try{initialize();setActive(activeName,mediaPaused,mediaReduced);}catch{lost=true;}});
    return {
      setActive,
      advance(dt){
        if(activeName!=='turell 5'||mediaPaused||mediaReduced||lost||!Number.isFinite(dt)||dt<=0)return;
        if(!solarPaused){solarClock=(solarClock+dt*solarSpeed)%OctetSolar.duration;solarMotion+=dt;}
      },
      setSolarOptions(options){
        if([.25,.5,1,2,4].includes(options.speed))solarSpeed=options.speed;
        if(['natural','cycle','yellow','amber','red'].includes(options.tone))solarTone=options.tone;
        if(['realism','artistic'].includes(options.mode))solarMode=options.mode;
      },
      performanceState(){
        return {renderer:rendererLabel,width:canvas.width,height:canvas.height,videos:activeMedia.map(index=>{
          const video=videos[index],quality=video.getVideoPlaybackQuality?.();
          return {name:mediaNames[index],presented:presentedFrames[index],paused:video.paused,
            dropped:quality?.droppedVideoFrames??null,total:quality?.totalVideoFrames??null,
            sourceFps:[29.97,60,30,30,25][index]};
        })};
      },
      mediaState(){
        if(activeName==='turell 5')return {kind:'eclipse',name:'solar',time:solarClock,duration:OctetSolar.duration,
          paused:solarPaused||mediaPaused||mediaReduced,seeking:false,zoom:solarZoom,speed:solarSpeed,tone:solarTone,mode:solarMode,phase:OctetSolar.at(solarClock,solarTone).label};
        if(activeMedia.length!==1)return null;
        const index=activeMedia[0],video=videos[index];
        return {name:mediaNames[index],time:video.currentTime,duration:video.duration,
          paused:video.paused||heldVideos.has(index),seeking:video.seeking,held:heldVideos.has(index),zoom:mediaZoom[index]};
      },
      setMediaZoom(value){
        if(activeName==='turell 5'){if(Number.isFinite(value))solarZoom=Math.max(.5,Math.min(3,value));return;}
        if(activeMedia.length!==1||!Number.isFinite(value))return;
        mediaZoom[activeMedia[0]]=Math.max(.5,Math.min(3,value));
      },
      seekMedia(seconds){
        if(activeName==='turell 5'){if(Number.isFinite(seconds))solarClock=Math.max(0,Math.min(OctetSolar.duration-.001,seconds));return;}
        if(activeMedia.length!==1||!Number.isFinite(seconds))return;
        const index=activeMedia[0],video=videos[index];
        if(!Number.isFinite(video.duration)||video.duration<=0)return;
        // Keep the last decoded texture/crop together while the new frame loads.
        const held=video.paused||heldVideos.has(index);
        video.currentTime=Math.max(0,Math.min(video.duration-.001,seconds));
        // Some Chromium decoders defer a paused seek until playback resumes.
        // Wake the muted decoder, then hold the requested decoded frame on
        // seeked; the UI continues to report the user's paused state.
        if(held&&!mediaPaused&&!mediaReduced){
          heldVideos.add(index);
          video.play().catch(()=>{});
        }
      },
      toggleMedia(){
        if(activeName==='turell 5'){solarPaused=!solarPaused;setActive(activeName,mediaPaused,mediaReduced);return;}
        if(activeMedia.length!==1)return;
        const index=activeMedia[0],video=videos[index];
        if(video.paused||heldVideos.has(index)){heldVideos.delete(index);setActive(activeName,mediaPaused,mediaReduced);}
        else{heldVideos.add(index);video.pause();}
      },
      resumeMedia(){setActive(activeName,document.hidden,mediaReduced);},
      resize(width,height) {
        // Desktop views allow 4K. The Pi supplies a smaller pixel budget for
        // smooth rendering, upscaled to the same full-screen HDMI viewport.
        const ratio=Math.min(devicePixelRatio||1,2,Math.sqrt(maxPixels/Math.max(1,width*height)),8192/Math.max(1,width),8192/Math.max(1,height));
        canvas.width=Math.max(1,Math.round(width*ratio));canvas.height=Math.max(1,Math.round(height*ratio));
        gl.viewport(0,0,canvas.width,canvas.height);
      },
      render(name,time,energy,peak,spectrum=[energy,energy,peak,.3],palette={field:[.56,.69,.14],halo:[1.,.82,.5],edge:[.7,.8,.3],edgeLight:.35}) {
        if(lost||gl.isContextLost())return;
        const id=themeIds[name];
        if(!id)return;
        activate(id);
        activeMedia.forEach(uploadVideo);
        gl.uniform2f(uniforms.resolution,canvas.width,canvas.height);
        gl.uniform1f(uniforms.time,time);gl.uniform1f(uniforms.energy,energy);gl.uniform1f(uniforms.peak,peak);
        gl.uniform1f(uniforms.videoZoom,id===10?solarZoom:activeMedia.length===1?mediaZoom[activeMedia[0]]:1);
        gl.uniform1f(uniforms.circleSize,palette.circleSize??1);
        gl.uniform4f(uniforms.spectrum,...spectrum);
        if(id===10){
          const state=OctetSolar.at(solarClock,solarTone);
          gl.uniform3f(uniforms.eclipseMoon,...state.moon);
          gl.uniform4f(uniforms.eclipseLight,solarMotion,state.corona,state.diamond,state.color);
          gl.uniform2f(uniforms.eclipseView,state.visible,solarMode==='realism'?1:0);
          gl.uniform4f(uniforms.sunFrame,...OctetSolar.sunViewAt(Math.max(0,lastTimes[3])));
        }
        gl.uniform3f(uniforms.fieldColor,...palette.field);gl.uniform3f(uniforms.haloColor,...palette.halo);
        gl.uniform3f(uniforms.edgeColor,...palette.edge);gl.uniform1f(uniforms.edgeLight,palette.edgeLight);
        const seedText=seeds[id%seeds.length];
        gl.uniform1f(uniforms.seed,(seedText.charCodeAt(0)+seedText.charCodeAt(31))*.01);
        gl.uniform1i(uniforms.theme,id);gl.drawArrays(gl.TRIANGLES,0,6);
      },
    };
  }
  return {descriptions,seeds,create};
})();
