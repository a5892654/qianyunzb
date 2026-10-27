// 离线浏览器交互回归。需安装 playwright；不连接直播平台。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
Object.assign(cfg.live2d, {enabled: true, model_file: 'fixture.model3.json'});
let previewing = false;
const requests = [], errors = [];
function merge(a, b) {
  for (const [key, value] of Object.entries(b)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) merge(a[key] ||= {}, value);
    else a[key] = value;
  }
}

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1320, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    await page.routeWebSocket('**/ws', ws => ws.onMessage(() => {}));
    await page.route('http://127.0.0.1:18787/**', async route => {
      const req = route.request(), url = new URL(req.url());
      const body = req.method() === 'POST' ? req.postDataJSON() : undefined;
      requests.push({url: url.pathname, body});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      if (url.pathname === '/live2d') return route.fulfill({contentType: 'text/html', body: '<body style="background:transparent">Live2D fixture</body>'});
      if (url.pathname === '/api/broadcast/stream.mjpg') return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#192d44"/><text x="45" y="65" font-size="26" fill="#8ab7d8">Game + Live2D composite fixture</text><ellipse cx="1060" cy="550" rx="80" ry="150" fill="#fb7299"/></svg>'});
      let response = {};
      if (url.pathname === '/api/config') {
        if (body) { assert.equal(req.headers()['x-bvb'], '1'); merge(cfg, body); response = {ok: true}; }
        else response = cfg;
      } else if (url.pathname === '/api/l2d/info') response = {cfg: cfg.live2d, expressions: [], motions: [], model_files: [cfg.live2d.model_file]};
      else if (url.pathname === '/api/tts/weights') response = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') response = [];
      else if (url.pathname === '/api/songs/state') response = {current: null, queue: [], credits: []};
      else if (url.pathname === '/api/broadcast/account') response = {ok: true, logged_in: false, room_id: 0};
      else if (url.pathname === '/api/broadcast/sources') response = {ok: true, windows: [{hwnd: 123, title: 'Fixture game'}], audio: [{id: 0, name: 'Fixture audio'}], default_window: 123, default_audio: 0};
      else if (url.pathname === '/api/broadcast/areas') response = {ok: true, areas: []};
      else if (url.pathname === '/api/broadcast/preview') { previewing = true; response = {ok: true}; }
      else if (url.pathname === '/api/broadcast/preview/stop') { previewing = false; response = {ok: true}; }
      else if (url.pathname === '/api/broadcast/status') response = {previewing, running: false, phase: previewing ? 'preview' : 'idle', overlay: previewing && cfg.live2d.enabled && cfg.live2d.overlay_in_broadcast, overlay_error: '', error: ''};
      else if (url.pathname === '/api/broadcast/schedule') response = {enabled: false, start_time: '20:00', stop_time: '23:00'};
      return route.fulfill({json: response});
    });
    await page.goto('http://127.0.0.1:18787/');
    await page.waitForFunction(() => document.querySelector('#l2dModelFile').options.length === 1);
    await page.click('#broadcastPreview');
    await page.waitForFunction(() => !!document.querySelector('#broadcastPreviewImage').getAttribute('src'));
    assert.ok(await page.locator('#overlayState').textContent().then(text => text.includes('正在叠加')));
    console.log('PASS preview requests the final composite stream with Live2D');

    await page.click('#openLayout');
    await page.waitForFunction(() => !!document.querySelector('#layoutPreviewImage').getAttribute('src'));
    assert.equal(await page.locator('#broadcastPreviewImage').getAttribute('src'), null);
    assert.equal(await page.locator('#layoutModelPreview').getAttribute('src'), null);
    assert.equal(await page.locator('#layoutPreviewImage').getAttribute('src').then(value => value.split('?')[0]), '/api/broadcast/stream.mjpg');
    console.log('PASS adjustment page uses the same composite stream');

    const before = {...cfg.live2d}, box = await page.locator('#layoutBox').boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 - 100, box.y + box.height / 2 - 40, {steps: 8});
    await page.mouse.up();
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(typeof cfg.live2d.overlay_x, 'number');
    assert.equal(typeof cfg.live2d.overlay_y, 'number');
    const movedX = cfg.live2d.overlay_x;
    assert.ok(movedX < 0.8);
    console.log('PASS dragging persists free position');

    const handle = await page.locator('#layoutResize').boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(handle.x + handle.width / 2 + 35, handle.y + handle.height / 2 + 45, {steps: 5});
    await page.mouse.up();
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.ok(cfg.live2d.overlay_size > before.overlay_size);
    console.log('PASS resize handle persists size');

    await page.locator('#layoutX').evaluate(el => { el.value = '30'; el.dispatchEvent(new Event('input', {bubbles: true})); });
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(cfg.live2d.overlay_x, 0.3);
    await page.selectOption('#overlayCorner', 'tl');
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(cfg.live2d.overlay_x, null);
    assert.equal(cfg.live2d.overlay_corner, 'tl');
    await page.click('#layoutReset');
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(cfg.live2d.overlay_corner, 'br');
    assert.equal(cfg.live2d.overlay_size, 0.5);
    console.log('PASS sliders, corner presets and reset persist');

    await page.click('#layoutBack');
    await page.locator('#broadcast [data-k="live2d.overlay_in_broadcast"]').uncheck();
    await page.waitForFunction(() => !document.querySelector('#layout [data-k="live2d.overlay_in_broadcast"]').checked);
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(cfg.live2d.overlay_in_broadcast, false);
    await page.locator('#broadcast [data-k="live2d.overlay_in_broadcast"]').check();
    await page.click('#broadcastPreviewStop');
    await page.click('#broadcastPreview');
    await page.waitForFunction(() => !!document.querySelector('#broadcastPreviewImage').getAttribute('src'));
    assert.ok(cfg.live2d.overlay_in_broadcast && cfg.live2d.enabled);
    const lastPreview = requests.findLastIndex(req => req.url === '/api/broadcast/preview');
    const lastSave = requests.findLastIndex(req => req.url === '/api/config' && req.body);
    assert.ok(lastSave < lastPreview);
    console.log('PASS shared switches and pending settings are saved before preview');

    await page.click('#openLayout');
    const screenshot = path.join(root, 'logs/live2d-layout-preview.png');
    await page.screenshot({path: screenshot, fullPage: true});
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#l2dModelFile').options.length === 1);
    await page.click('nav [data-tab="layout"]');
    assert.equal(await page.locator('#layoutSize').inputValue(), '0.5');
    assert.deepEqual(errors, []);
    console.log('PASS saved settings survive reload; no JavaScript errors');
    console.log('Screenshot:', screenshot);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
