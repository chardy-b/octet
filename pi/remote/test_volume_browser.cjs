/* Nonlinear listening-level controls in a real browser; all audio APIs are mocked. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const decoded = (position, min = -60, max = -18) =>
  Math.round((min + (max - min) * Math.log1p(9 * position / 1000) / Math.log(10)) * 10) / 10;
const position = (db, min = -60, max = -18) =>
  Math.expm1((db - min) / (max - min) * Math.log(10)) / 9 * 1000;
const initial = () => ({
  ready: true, source: 'tidal', source_label: 'Tidal', source_switching_available: true,
  sources: [{id: 'tidal', label: 'Tidal', group: 'Music', available: true}],
  muted: true, volume: -36, min_db: -60, max_db: -18, level_unlocked: false,
  profile: 'Saved listening profile', profile_description: 'Current speaker tuning',
  playback: 'pause', duration: 0, elapsed: 0, queue: [], channels: [], sample_rate: 48000,
});

async function checkPage(browser, kind) {
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const errors = [], writes = [];
  let state = initial();
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://octet.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/status' || url.pathname === '/api/dsp') return route.fulfill({json: state});
    if (url.pathname === '/api/auto') return route.fulfill({json: {available: false, profiles: [], audio_owned: false}});
    if (url.pathname === '/api/input-levels') return route.fulfill({json: {inputs: [], outputs: [], age_ms: 0, message: 'No USB input connected.'}});
    if (url.pathname === '/api/favorites') return route.fulfill({json: {tracks: []}});
    if (url.pathname.startsWith('/api/control/')) {
      const body = route.request().postDataJSON();
      writes.push({action: url.pathname.split('/').at(-1), body});
      if (url.pathname === '/api/control/volume') state = {...state, volume: body.db};
      else if (url.pathname === '/api/control/unlock') {
        state = {...state, level_unlocked: body.enabled, max_db: body.enabled ? 0 : -18};
        if (state.volume > state.max_db) state.volume = state.max_db;
      } else throw Error('Unexpected audio mutation: ' + url.pathname);
      return route.fulfill({json: state});
    }
    const file = url.pathname === '/' ? 'index.html' : url.pathname === '/dsp' ? 'dsp.html' : url.pathname.slice(1);
    const target = path.join(__dirname, file);
    if (!fs.existsSync(target)) return route.fulfill({status: 404, body: ''});
    return route.fulfill({body: fs.readFileSync(target), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'});
  });
  try {
    await page.goto('http://octet.test/' + (kind === 'dsp' ? 'dsp' : ''));
    const slider = page.locator('#volume');
    await page.waitForFunction(() => document.getElementById('connection').textContent.includes('online'));
    assert.equal(await slider.getAttribute('min'), '0');
    assert.equal(await slider.getAttribute('max'), '1000');
    assert.ok(Math.abs(Number(await slider.inputValue()) - position(-36)) <= 0.6, kind + ': readback uses inverse curve');
    assert.match((await slider.getAttribute('aria-valuetext')).replaceAll('−', '-'), /-36(?:\.0)? dB/);
    assert.equal(writes.length, 0, kind + ': initial render does not change audio');

    await slider.evaluate(node => {node.value = '500'; node.dispatchEvent(new Event('input', {bubbles: true}));});
    assert.match((await page.locator('#volume-value').textContent()).replaceAll('−', '-'), /-28\.9 dB/);
    assert.match((await slider.getAttribute('aria-valuetext')).replaceAll('−', '-'), /-28\.9 dB/);
    assert.equal(writes.length, 0, kind + ': preview does not commit audio');
    await page.evaluate(() => refresh());
    assert.equal(await slider.inputValue(), '500', kind + ': polling preserves a drag in progress');
    const firstCommit = page.waitForResponse(response => new URL(response.url()).pathname === '/api/control/volume');
    await slider.dispatchEvent('change');
    await firstCommit;
    await page.waitForFunction(kind === 'dsp' ? () => !busy : () => !volumeBusy);
    assert.deepEqual(writes[0], {action: 'volume', body: {db: decoded(500)}});
    assert.equal(state.volume, -28.9);

    await page.locator('#unlock-level').click();
    await page.waitForFunction(() => document.getElementById('unlock-level').getAttribute('aria-pressed') === 'true');
    assert.equal(state.volume, -28.9, kind + ': unlocking preserves the actual level');
    assert.equal(writes.filter(write => write.action === 'volume').length, 1);
    assert.ok(Math.abs(Number(await slider.inputValue()) - position(-28.9, -60, 0)) <= 0.6, kind + ': range change remaps the thumb');
    assert.match((await slider.getAttribute('aria-valuetext')).replaceAll('−', '-'), /-28\.9 dB/);

    async function key(key, expectedDb) {
      const committed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/control/volume');
      await slider.press(key);
      await committed;
      await page.waitForFunction(kind === 'dsp' ? () => !busy : () => !volumeBusy);
      assert.equal(writes.at(-1).body.db, expectedDb, kind + ': ' + key + ' submits dB');
      assert.equal(state.volume, expectedDb);
    }
    await key('ArrowRight', -27.9);
    await key('End', 0);
    await key('Home', -60);
    await key('PageUp', -55);
    assert.ok(Math.abs(Number(await slider.inputValue()) - position(-55, -60, 0)) <= 0.06);

    state = {...state, volume: -24};
    await page.evaluate(() => refresh());
    const volumeWrites = writes.filter(write => write.action === 'volume').length;
    await page.locator('#unlock-level').click();
    await page.waitForFunction(() => document.getElementById('unlock-level').getAttribute('aria-pressed') === 'false');
    assert.equal(state.volume, -24, kind + ': relocking keeps a level below the ceiling');
    assert.equal(writes.filter(write => write.action === 'volume').length, volumeWrites);
    assert.ok(Math.abs(Number(await slider.inputValue()) - position(-24)) <= 0.6);
    await key('End', -18);
    await key('PageDown', -23);
    assert.equal(state.muted, true, kind + ': level controls do not alter mute');
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    await checkPage(browser, 'music');
    await checkPage(browser, 'dsp');
    console.log('Volume browser checks passed: both pages preserve dB on render and unlock, use the nonlinear curve, retain drags across polls, expose dB to assistive technology, and support dB keyboard steps.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
