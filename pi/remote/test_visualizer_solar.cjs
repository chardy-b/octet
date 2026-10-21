const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'visualizer-solar.js'),'utf8'),context);
const solar=vm.runInContext('OctetSolar',context);
assert.equal(solar.at(0).visible,1);assert.equal(solar.at(solar.duration).visible,1);
assert.equal(solar.at(solar.duration/2).visible,0);assert.equal(solar.at(solar.duration/2).corona,1);
assert.equal(solar.at(solar.duration/2).diamond,0);assert.equal(solar.at(solar.duration/2).label,'Totality');
assert.equal(solar.at(0,'natural').color,-1);assert.equal(solar.at(0,'yellow').color,0);
assert.equal(solar.at(0,'red').color,1);assert.equal(solar.at(solar.duration/2,'cycle').color,1);
assert.equal(solar.duration,720);
for(let time=240;time<=480;time++)assert.equal(solar.at(time).visible,0);
const diamond=solar.at(solar.landmarks.diamond);
assert.ok(diamond.visible>0&&diamond.visible<.01);assert.ok(diamond.diamond>.999);
assert.ok(solar.at(solar.landmarks.egress).diamond>.999);
let previous=1;
for(let time=0;time<=solar.duration/2;time+=.1){
  const state=solar.at(time);
  assert.ok(state.visible<=previous+1e-8);previous=state.visible;
  assert.ok([state.visible,state.corona,state.diamond,...state.moon].every(Number.isFinite));
  assert.ok(state.visible>=0&&state.visible<=1);
}
for(const phase of [.08,1/3,2/3,.92]){
  const a=solar.at(phase*solar.duration-.0001),b=solar.at(phase*solar.duration+.0001);
  assert.ok(Math.hypot(a.moon[0]-b.moon[0],a.moon[1]-b.moon[1])<.0001);
}
for(let t=0;t<=18;t+=.1){
  const [x,y,w,h]=solar.sunViewAt(t);
  assert.ok(x-w/2>=0&&x+w/2<=1&&y-h/2>=0&&y+h/2<=1);
  assert.ok(Math.abs(w*1920-h*1080)<1e-6);
}
console.log('Solar checks passed: continuous contact geometry, extended totality, occultation, diamond timing, color comparison, and native-source crop.');
