'use strict';
(()=>{
  const SCALE=1000, CURVE=9;
  const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
  function positionToDb(position,min=-60,max=-18){
    return min+(max-min)*Math.log1p(CURVE*clamp(Number(position)/SCALE,0,1))/Math.log1p(CURVE);
  }
  function dbToPosition(db,min=-60,max=-18){
    return SCALE*Math.expm1(clamp((Number(db)-min)/(max-min),0,1)*Math.log1p(CURVE))/CURVE;
  }
  const rounded=db=>Math.round(db*10)/10;
  const label=db=>rounded(db)+' dB';
  function create(slider,readout){
    let min=-60,max=-18;
    slider.min='0';slider.max=String(SCALE);slider.step='0.1';
    function value(){return rounded(positionToDb(slider.value,min,max));}
    function show(db){
      readout.textContent=label(db);
      slider.setAttribute('aria-valuemin',String(min));
      slider.setAttribute('aria-valuemax',String(max));
      slider.setAttribute('aria-valuenow',String(rounded(db)));
      slider.setAttribute('aria-valuetext',label(db));
    }
    function preview(){const db=value();show(db);return db;}
    function render(state,{busy=false}={}){
      const pending=value(),oldMin=min,oldMax=max;
      min=Number.isFinite(state.min_db)?state.min_db:-60;
      max=Number.isFinite(state.max_db)?state.max_db:-18;
      if(!busy){
        slider.value=String(dbToPosition(state.volume,min,max));
        show(state.volume);
      }else if(min!==oldMin||max!==oldMax){
        slider.value=String(dbToPosition(pending,min,max));preview();
      }
    }
    slider.addEventListener('keydown',event=>{
      if(slider.disabled)return;
      const changes={ArrowRight:1,ArrowUp:1,ArrowLeft:-1,ArrowDown:-1,PageUp:5,PageDown:-5};
      if(!(event.key in changes)&&event.key!=='Home'&&event.key!=='End')return;
      event.preventDefault();
      const db=event.key==='Home'?min:event.key==='End'?max:clamp(value()+changes[event.key],min,max);
      slider.value=String(dbToPosition(db,min,max));
      slider.dispatchEvent(new Event('input',{bubbles:true}));
      slider.dispatchEvent(new Event('change',{bubbles:true}));
    });
    return {render,preview,value};
  }
  const api=Object.freeze({positionToDb,dbToPosition,create});
  if(typeof window!=='undefined')window.OctetVolume=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
})();
