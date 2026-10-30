const assert=require('node:assert/strict');
const {positionToDb,dbToPosition}=require('./volume.js');
for(const [min,max] of [[-60,-18],[-60,0]]){
  assert.equal(positionToDb(0,min,max),min);
  assert(Math.abs(positionToDb(1000,min,max)-max)<1e-12);
  assert.equal(dbToPosition(min,min,max),0);
  assert(Math.abs(dbToPosition(max,min,max)-1000)<1e-9);
  let previous=min;
  for(let position=0;position<=1000;position++){
    const db=positionToDb(position,min,max);
    assert(db>=previous,'The volume must increase monotonically.');
    assert(Math.abs(dbToPosition(db,min,max)-position)<1e-9,'The display must invert the control mapping.');
    previous=db;
  }
  assert(positionToDb(500,min,max)>min+(max-min)*.7,'Most slider travel must cover the louder dB range.');
  assert(positionToDb(750,min,max)-positionToDb(500,min,max)<positionToDb(250,min,max)-min);
  assert.equal(positionToDb(-100,min,max),min);
  assert(Math.abs(positionToDb(1100,min,max)-max)<1e-12);
  assert.equal(dbToPosition(min-10,min,max),0);
  assert(Math.abs(dbToPosition(max+10,min,max)-1000)<1e-9);
}
console.log('Volume curve checks passed: endpoints, monotonicity, inverse, clamping and finer louder-range travel.');
