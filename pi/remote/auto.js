'use strict';
(() => {
  const el = id => document.getElementById(id);
  let status = {}, pending = false, signature = '', selectionLoaded = false, testLevelLoaded = false;
  const pairInputs = [...document.querySelectorAll('input[name="auto-pair"]')];
  const selectedPairs = () => pairInputs.filter(node => node.checked).map(node => node.value);
  const validPairs = () => selectedPairs().some(role => role === 'a' || role === 'b');
  const pairNames = {a:'A (1/2)',b:'B (3/4)',sub:'Subs (5/6)',tweeter:'Tweeters (7/8)'};
  function choiceSummary(choice, pairs) {
    const parts = pairs.map(role => pairNames[role]);
    if(choice.strategy === 'protected') return parts.join(' · ')+' · protected driver EQ';
    if(pairs.includes('sub')) parts.push(`${Math.round(choice.low)} Hz sub / exciter`);
    if(pairs.includes('tweeter')) parts.push(`${Math.round(choice.high)} Hz exciter / tweeter`);
    parts.push(`LR${choice.order}`);
    return parts.join(' · ');
  }
  const roles = ['EX30 · left','EX30 · right','EX32 · left','EX32 · right','Sub · left','Sub · right','Tweeter · left','Tweeter · right'];
  window.renderTunedOutputs = data => {
    const root = el('channels'); root.replaceChildren();
    el('eq-count').textContent = 'Saved system profile · corrections and safety filters active';
    el('enable-all').disabled = true;
    roles.forEach((name, index) => {
      const card = document.createElement('article'); card.className = 'channel-card';
      const title = document.createElement('h2'); title.textContent = `${index + 1}. ${name}`;
      const detail = document.createElement('p'); detail.className = 'channel-note';
      const side = index % 2 ? 'R' : 'L', role = index < 2 ? 'a' : index < 4 ? 'b' : index < 6 ? 'sub' : 'tweeter';
      const choice = data.tuning.chosen?.[side];
      const stereo = data.tuning.stereo?.[side] || {};
      const pairs = data.tuning.selected_pairs || choice?.active_pairs || ['a','b','sub','tweeter'];
      const driverId = `${side}_${role}`, failure = data.tuning.failed_drivers?.[driverId];
      const usable = data.tuning.active_driver_ids ? data.tuning.active_driver_ids.includes(driverId) : pairs.includes(role);
      detail.textContent = failure ? `Measurement skipped · output muted. ${failure.reason}` : !usable ? 'Not selected for this profile · output muted.' : choice ? `${choiceSummary(choice,choice.active_pairs || pairs)} · ${(choice.gains[role]+(stereo.gain_db || 0)).toFixed(1)} dB relative attenuation · ${(choice.delays[role]+(stereo.delay_ms || 0)).toFixed(2)} ms delay · ${choice.polarity[role]*(stereo.polarity || 1) < 0 ? 'inverted' : 'normal'} polarity` : 'Saved settings active. Select another system profile to change the complete tuning.';
      card.append(title, detail);
      if (role === 'tweeter') {const protection = document.createElement('p'); protection.className = 'auto-protection'; protection.textContent = 'Independent 3.5 kHz minimum safety high-pass remains active.';card.append(protection);}
      root.append(card);
    });
  };
  function render(data) {
    status = data;
    if(!testLevelLoaded && data.available) {
      el('auto-test-attenuation').value = data.test_attenuation_db ?? 0;
      testLevelLoaded = true;
    }
    if(!selectionLoaded && data.available) {
      const pairs = data.selected_pairs || ['a','b','sub','tweeter'];
      pairInputs.forEach(node => {node.checked = pairs.includes(node.value);});
      selectionLoaded = true;
    }
    window.autoAudioOwned = Boolean(data.audio_owned);
    if(data.audio_owned) el('global-mute').textContent='Stop tones';
    el('auto-status').textContent = data.message || data.status;
    el('auto-status').classList.toggle('error', ['failed','cancelled','interrupted'].includes(data.status));
    const skipped = el('auto-skipped-list');skipped.replaceChildren();
    const failures = Object.values(data.failed_drivers || {});
    el('auto-skipped').hidden = !failures.length;
    for(const failure of failures.sort((a,b)=>a.output-b.output)) {
      const row = document.createElement('li');row.textContent = `Output ${failure.output}: ${failure.reason} Excluded from this run.`;skipped.append(row);
    }
    el('auto-meter').textContent = Number.isFinite(data.peak_db) ? `Microphone peak: ${data.peak_db.toFixed(1)} dBFS` : '';
    el('auto-start').disabled = pending || !data.available || data.audio_owned || data.running || !validPairs();
    el('auto-stop').disabled = pending || !data.running;
    el('auto-restore').disabled = pending || data.running || !(data.profiles || []).length;
    el('auto-name').disabled = data.running;
    el('auto-ready').disabled = data.running;
    el('auto-pairs').disabled = pending || data.running;
    el('auto-test-attenuation').disabled = pending || data.running;
    el('auto-test-save').disabled = pending || data.running || !data.available;
    const listSignature = JSON.stringify([data.profiles, data.active?.id]);
    if (signature !== listSignature) {
      signature = listSignature;
      const select = el('auto-profiles'), prior = select.value;
      select.replaceChildren();
      if (!(data.profiles || []).length) select.add(new Option('No saved tuning profiles yet', ''));
      for (const profile of data.profiles || []) select.add(new Option(profile.name + (profile.id === data.active?.id ? ' · active' : ''), profile.id));
      if ([...select.options].some(option => option.value === prior)) select.value = prior;
      else if (data.active) select.value = data.active.id;
    }
    el('auto-select').disabled = pending || data.running || data.audio_owned || !el('auto-profiles').value;
    el('auto-profiles').disabled = pending;
    window.profileMeasurementViewer?.update();
    if (data.active?.chosen) el('auto-summary').textContent = ['L','R'].map(side => {const c=data.active.chosen[side];return c ? `${side}: ${choiceSummary(c,c.active_pairs || data.active.selected_pairs || ['a','b','sub','tweeter'])}` : `${side}: no measurable outputs · muted`;}).join(' · ') + '. Saved with EQ, attenuation, delay, polarity and output ceilings.';
    if(data.active?.chosen && Number.isFinite(data.active.playback_max_db)) {
      el('auto-summary').textContent += ` Music output ceiling: ${data.active.playback_max_db} dBFS.`;
      if(data.active.preamp_db) el('auto-summary').textContent += ' Overall attenuation: '+['L','R'].map(side=>`${side} ${(data.active.preamp_db[side]+(data.active.stereo?.[side]?.gain_db || 0)).toFixed(1)} dB`).join(' / ')+'.';
    }
    if(data.active?.stereo_verification?.passed) {
      const check = data.active.active_stereo_verification || data.active.stereo_verification;
      el('auto-summary').textContent += ` Stereo timing, balance and polarity verified (${check.right_minus_left_ms.toFixed(2)} ms, ${check.right_minus_left_db.toFixed(1)} dB). Three captures averaged at each measurement stage.`;
    }
    if(data.active?.stereo_skipped) el('auto-summary').textContent += ' Stereo alignment skipped: '+data.active.stereo_skipped;
    if(data.active?.measurement_mode === 'magnitude_only_recovery') el('auto-summary').textContent = data.active.verification_basis;
    document.querySelectorAll('#channels button,#channels input,#enable-all,#volume,#unlock-level').forEach(node => {if (data.audio_owned) {node.dataset.autoLocked = 'true';node.disabled = true;} else if (node.dataset.autoLocked) {delete node.dataset.autoLocked;node.disabled = false;}});
  }
  async function refresh() {
    try {const response = await fetch('/api/auto');if (!response.ok) throw Error('Auto Tune service unavailable');render(await response.json());}
    catch (error) {render({...status,available:false,message:error.message});}
  }
  async function action(name, body = {}) {
    pending = true;render(status);
    try {
      const response = await fetch('/api/control/auto-' + name, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data = await response.json();if (!response.ok) throw Error(data.error || 'Auto mode action failed');
      render(data);
    } catch (error) {el('auto-status').textContent = error.message;el('auto-status').classList.add('error');}
    finally {pending = false;el('auto-pairs').disabled = status.running;el('auto-test-attenuation').disabled = status.running;el('auto-test-save').disabled = status.running || !status.available;el('auto-start').disabled = !status.available || status.audio_owned || status.running || !validPairs();el('auto-stop').disabled = !status.running;el('auto-restore').disabled = status.running || !(status.profiles || []).length;el('auto-select').disabled = status.running || status.audio_owned || !el('auto-profiles').value;}
  }
  pairInputs.forEach(node => node.onchange = () => render(status));
  el('auto-test-save').onclick = () => {if(el('auto-test-attenuation').reportValidity())action('test-level',{test_attenuation_db:Number(el('auto-test-attenuation').value)});};
  el('auto-start-form').onsubmit = event => {event.preventDefault();if(validPairs())action('start',{name:el('auto-name').value,microphone_ready:el('auto-ready').checked,selected_pairs:selectedPairs(),test_attenuation_db:Number(el('auto-test-attenuation').value)});};
  el('auto-stop').onclick = () => action('stop');
  el('auto-restore').onclick = () => action('restore');
  el('auto-profile-form').onsubmit = event => {event.preventDefault();action('select',{id:el('auto-profiles').value});};
  refresh();setInterval(refresh,1500);
})();
