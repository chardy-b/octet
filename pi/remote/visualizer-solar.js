'use strict';
// Installation timing stretches totality. Contact geometry follows two discs;
// the yellow/red palette is an artistic solar-filter treatment, not cooling.
const OctetSolar=(()=>{
  const duration=720,moonRadius=1.032;
  const clamp=(v,a=0,b=1)=>Math.max(a,Math.min(b,v));
  const ease=v=>{v=clamp(v);return v*v*(3-2*v);};
  function at(seconds,tone='natural'){
    const phase=clamp(Number.isFinite(seconds)?seconds/duration:0);
    let x;
    if(phase<.08)x=-2.5;
    else if(phase<1/3)x=-2.5+2.482*ease((phase-.08)/(1/3-.08));
    else if(phase<2/3)x=-.018+.036*ease((phase-1/3)/(1/3));
    else if(phase<.92)x=.018+2.482*ease((phase-2/3)/(.92-2/3));
    else x=2.5;
    const y=.004,d=Math.hypot(x,y),R=moonRadius;
    let visible=1;
    if(d<=R-1)visible=0;
    else if(d<1+R){
      const a=Math.acos(clamp((d*d+1-R*R)/(2*d),-1,1));
      const b=Math.acos(clamp((d*d+R*R-1)/(2*d*R),-1,1));
      const lens=a+R*R*b-.5*Math.sqrt(Math.max(0,(-d+1+R)*(d+1-R)*(d-1+R)*(d+1+R)));
      visible=clamp(1-lens/Math.PI);
    }
    const corona=1-ease((visible-.0001)/.025);
    const diamond=visible>0?Math.exp(-Math.pow((d-(R-1+.016))/.020,2)):0;
    const color=tone==='natural'?-1:({yellow:0,amber:.48,red:1}[tone]??(.5-.5*Math.cos(phase*Math.PI*2)));
    const label=visible===0?'Totality':diamond>.38?'Diamond ring / Baily’s beads':visible>.999?'Unobscured Sun':phase<.5?'Ingress':'Egress';
    return {phase,moon:[x,y,R],visible,corona,diamond,color,label};
  }
  let lo=0,hi=duration/3;
  for(let i=0;i<40;i++){const mid=(lo+hi)/2;if(Math.hypot(...at(mid).moon.slice(0,2))>moonRadius-1+.016)lo=mid;else hi=mid;}
  const landmarks={sun:0,diamond:(lo+hi)/2,totality:duration/2,egress:duration-(lo+hi)/2};
  function sunViewAt(seconds){
    const t=clamp(Number.isFinite(seconds)?seconds/18:0);
    return [(895+4*t)/1920,1-(506+1*t)/1080,868/1920,868/1080];
  }
  const glsl=`
uniform vec3 eclipseMoon;
uniform vec4 eclipseLight; // animation seconds, corona exposure, diamond, palette
uniform vec2 eclipseView; // uncovered solar area, realism mode
uniform vec4 sunFrame;
float solarSpot(vec3 n,vec3 center,float size){
  float d=sqrt(max(0.,2.-2.*dot(n,normalize(center))));
  float ragged=1.+.10*sin(d*620.)+.06*sin(n.y*360.);
  float penumbra=1.-smoothstep(size*.65,size*1.8,d*ragged);
  float umbra=1.-smoothstep(size*.32,size*.70,d*ragged);
  return penumbra*.45+umbra*.48;
}
vec3 solarSurface(vec2 q){
  float z=sqrt(max(0.,1.-dot(q,q))),clock=eclipseLight.x;
  vec3 n=spinY(vec3(q,z),clock*.0009);
  float spots=solarSpot(n,vec3(-.38,.24,1.),.047)+solarSpot(n,vec3(-.31,.19,1.),.022)
    +solarSpot(n,vec3(.26,-.18,1.),.036)+solarSpot(n,vec3(.32,-.15,1.),.018)
    +solarSpot(n,vec3(.55,.32,1.),.026)+solarSpot(n,vec3(-.14,-.39,1.),.016);
  float tone=eclipseLight.w;
  vec3 yellow=vec3(1.,.86,.28),amber=vec3(1.,.37,.035),red=vec3(.75,.045,.009);
  vec3 captured=texture2D(sun,q*.5*sunFrame.zw+sunFrame.xy).rgb;
  // The real unobscured telescope disc provides filaments, active regions and
  // fine surface motion. Add only restrained modeled sunspot groups.
  float detail=1.-clamp(spots*.38,0.,.60);
  if(tone<0.)return captured*detail*(1.+energy*.018);
  vec3 color=tone<.5?mix(yellow,amber,tone*2.):mix(amber,red,(tone-.5)*2.);
  float brightness=pow(max(captured.r,max(captured.g,captured.b)),1.12);
  return color*brightness*detail*(1.04+energy*.025);
}
float solarProminence(float r,float angle,float center,float span,float height){
  float delta=atan(sin(angle-center),cos(angle-center));
  float t=delta/span;if(abs(t)>1.)return 0.;
  float arch=1.005+height*pow(max(0.,1.-t*t),.66);
  float strand=exp(-abs(r-arch)/.006)+.20*exp(-abs(r-arch+.012)/.012);
  return strand*(.78+.22*sin(t*12.+eclipseLight.x*.13));
}
vec3 proceduralSolar(vec2 p){
  float outer=length(p);if(outer>1.)return vec3(0.);
  float aperture=.28*videoZoom;
  vec2 q=p/aperture;float r=length(q),angle=atan(q.y,q.x);
  float aa=max(.0004,2./(min(resolution.x,resolution.y)*aperture));
  float total=eclipseLight.y,clock=eclipseLight.x;
  vec3 field=fieldColor*(.81+.24*exp(-dot(p-vec2(0.,-.23),p-vec2(0.,-.23))/.75));
  // Sample the lunar shadow at this angle of the solar limb. The colored halo
  // recedes with the covered crescent rather than glowing through the Moon.
  vec2 rimPoint=vec2(cos(angle),sin(angle))*.995;
  float rimLit=smoothstep(-.008,.025,length(rimPoint-eclipseMoon.xy)-eclipseMoon.z);
  float halo=exp(-pow(max(outer-aperture,0.)/(.12+energy*.012),2.))*.45;
  halo*=(.08+.92*rimLit)*pow(eclipseView.x,.35)*(1.-total);
  field=mix(field,haloColor,halo);
  float edge=edgeLight*(.20*exp(-pow((1.-outer)/.18,2.))+.56*exp(-pow((1.-outer)/.045,2.)));
  float ambient=mix(.25,.025,eclipseView.y);
  field=mix(field,edgeColor,edge)*(ambient+(1.-ambient)*pow(eclipseView.x,.40));
  float moonAngle=atan(q.y-eclipseMoon.y,q.x-eclipseMoon.x);
  float mountains=.0013*sin(moonAngle*31.+.7)+.0007*sin(moonAngle*67.-.5)+.0004*sin(moonAngle*113.);
  float moonDistance=length(q-eclipseMoon.xy)-eclipseMoon.z-mountains;
  float outsideMoon=smoothstep(-aa,aa,moonDistance);
  float disc=1.-smoothstep(1.-aa,1.+aa,r);
  vec3 surface=vec3(0.);if(r<1.+aa&&outsideMoon>.001)surface=solarSurface(q);
  // A black lunar limb extends a little beyond the solar limb during totality.
  float lunarEdge=(1.-outsideMoon)*(1.-smoothstep(1.035,1.07,r));
  vec3 result=surface*outsideMoon*disc+field*(1.-disc)*(1.-lunarEdge);
  if(r>.98){
    float d=max(r-1.,0.);
    // Small, slow angular bends and outward-flowing filaments keep the corona
    // alive without rotating the whole crown or changing the lunar geometry.
    float bend=.014*sin(angle*3.+clock*.075)+.009*sin(angle*7.-clock*.043);
    float a=angle+bend*(1.-exp(-d*1.8));
    float breath=1.+.035*sin(clock*.16)+.018*sin(clock*.071+1.3);
    float width=(.38+.44*pow(abs(cos(a+.18)),4.)+.05*sin(a*3.+.4))
      *(1.+.045*sin(clock*.095+a*4.));
    float rays=.88+.075*sin(a*23.+sin(a*7.)*2.+.22*sin(clock*.11-d*2.))
      +.045*sin(a*57.+.6+.16*sin(clock*.083-d*3.));
    float flow=1.+.045*sin(d*17.-clock*.22+sin(a*11.));
    float corona=(1.18*exp(-d/.08)+.66*exp(-d/width)+.09*exp(-d/1.25))*rays*breath*flow;
    corona*=smoothstep(.985,1.015,r)*outsideMoon*total;
    result+=vec3(.88,.93,1.)*corona*(1.+energy*.035);
    float chromosphere=exp(-abs(r-1.006)/.008)*outsideMoon*total;
    float prominences=solarProminence(r,angle,-1.03,.11,.19)+solarProminence(r,angle,1.88,.15,.12)
      +solarProminence(r,angle,2.72,.07,.23)+solarProminence(r,angle,.34,.10,.10);
    result+=vec3(1.,.035,.095)*(chromosphere*.48+prominences*(.16+total*.70))*outsideMoon;
  }
  // Sunlight through lunar valleys: several beads, resolving to one diamond.
  float diamond=eclipseLight.z;
  if(diamond>.001){
    vec2 direction=-normalize(eclipseMoon.xy),star=direction*1.002;
    vec2 v=q-star;float h=length(v);
    float core=exp(-h*h/.00055)*6.;
    float bloom=1.1*exp(-h/.07)+.09*exp(-h/.34);
    float spikes=.65*exp(-abs(v.x)/.005-abs(v.y)/.28)+.65*exp(-abs(v.y)/.005-abs(v.x)/.28);
    float beads=0.;
    for(int i=-3;i<=3;i++){
      float a=atan(direction.y,direction.x)+float(i)*.038;
      vec2 bead=vec2(cos(a),sin(a))*(1.+.001*sin(float(i)*4.));
      float localMoon=length(bead-eclipseMoon.xy)-eclipseMoon.z;
      float valley=smoothstep(-.002,.003,localMoon+.0015*sin(float(i)*7.));
      beads+=exp(-dot(q-bead,q-bead)/.00009)*valley;
    }
    result+=vec3(1.,.96,.87)*(core+bloom+spikes+beads*.65)*diamond;
  }
  return result;
}
`;
  return {duration,moonRadius,at,landmarks,sunViewAt,glsl};
})();
