// 离线浏览器交互回归。需安装 playwright；不连接直播平台。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
Object.assign(cfg.live2d, {enabled: true, model_dir: 'fixture', model_file: 'fixture.model3.json', emotions: [
  {name: '生气', expressions: [], motion: '', keywords: ''},
  {name: '开心', expressions: ['stars'], motion: 'nod', keywords: ''},
]});
let previewing = false;
let currentModel = 'fixture', rejectSwitch = false;
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
      if (url.pathname === '/rice') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/rice.html'), 'utf8')});
      if (url.pathname === '/static/assets/rice_bowl_celebrate.png') return route.fulfill({contentType: 'image/png', body: fs.readFileSync(path.join(root, 'web/assets/rice_bowl_celebrate.png'))});
      if (url.pathname === '/static/assets/ocean-background.png') return route.fulfill({contentType: 'image/png', body: fs.readFileSync(path.join(root, 'web/assets/ocean-background.png'))});
      if (url.pathname === '/api/theme/preview.jpg') return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080"><rect width="1920" height="1080" fill="#125681"/><rect x="466" y="150" width="1410" height="793" fill="#10273a"/></svg>'});
      if (url.pathname === '/api/broadcast/stream.mjpg') return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#192d44"/><text x="45" y="65" font-size="26" fill="#8ab7d8">Game + Live2D composite fixture</text><ellipse cx="1060" cy="550" rx="80" ry="150" fill="#fb7299"/></svg>'});
      let response = {};
      if (url.pathname === '/api/config') {
        if (body) { assert.equal(req.headers()['x-bvb'], '1'); merge(cfg, body); response = {ok: true}; }
        else response = cfg;
      } else if (url.pathname === '/api/l2d/info') response = {cfg: cfg.live2d, expressions: [{id: 'stars', label: '星星眼'}], motions: [{id: 'nod', label: '点头'}], model_files: [cfg.live2d.model_file]};
      else if (url.pathname === '/api/l2d/library') response = {root: 'fixture-library', current: currentModel, models: [{key: 'fixture'}, {key: 'second'}]};
      else if (url.pathname === '/api/l2d/test') response = {ok: true};
      else if (url.pathname === '/api/rice/state') response = {enabled: cfg.rice_counter.enabled, total: 0, celebrated_at: 0, layout: {x: cfg.rice_counter.overlay_x, y: cfg.rice_counter.overlay_y, size: cfg.rice_counter.overlay_size}};
      else if (url.pathname === '/api/l2d/switch') {
        if (rejectSwitch) response = {ok: false, error: 'fixture switch failed'};
        else {
          currentModel = body.key;
          Object.assign(cfg.live2d, {model_dir: body.key, model_file: body.key + '.model3.json'});
          response = {ok: true};
        }
      }
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
    await page.click('nav [data-tab="l2d"]');
    assert.ok(await page.locator('#l2dEmotionTestBtn').isVisible());
    const quickTest = await page.locator('#l2dEmotionTestBtn').boundingBox();
    assert.ok(quickTest.y + quickTest.height < 1000, 'Quick test must be visible before the long emotion editor');
    assert.ok((await page.locator('#l2dTestPreview').getAttribute('src')).startsWith('/live2d?'));
    await page.selectOption('#l2dEmotionTest', '1');
    const quickRequest = page.waitForRequest(req => req.url().endsWith('/api/l2d/test'));
    await page.getByRole('button', {name: '测试 AI 表演', exact: true}).click();
    assert.deepEqual((await quickRequest).postDataJSON(), {emotion: '开心', expressions: ['stars'], motion: 'nod', text: '（开心）'});
    const resetRequest = page.waitForRequest(req => req.url().endsWith('/api/l2d/test'));
    await page.getByRole('button', {name: '恢复默认状态', exact: true}).click();
    assert.deepEqual((await resetRequest).postDataJSON(), {motion: '@reset'});
    await page.waitForFunction(() => document.querySelector('#l2dTestStatus').textContent.includes('已恢复默认状态'));
    assert.ok((await page.locator('#l2dTestSummary').textContent()).includes('AI 已接入 1/1 个表情、1/1 个动作'));
    console.log('PASS visible quick-test buttons and inline preview use the selected AI performance');
    for (const [index, emotion] of cfg.live2d.emotions.entries()) {
      const sent = page.waitForRequest(req => req.url().endsWith('/api/l2d/test'));
      await page.click(`[data-etest="${index}"]`);
      assert.deepEqual((await sent).postDataJSON(), {emotion: emotion.name, expressions: emotion.expressions, motion: emotion.motion, text: `（${emotion.name}）`});
    }
    for (const [selector, expected] of [['[data-texpr="stars"]', {expressions: ['stars']}], ['[data-tmotion="nod"]', {motion: 'nod'}]]) {
      const sent = page.waitForRequest(req => req.url().endsWith('/api/l2d/test'));
      await page.click(selector);
      assert.deepEqual((await sent).postDataJSON(), expected);
    }
    console.log('PASS emotion preview sends the name; raw expression and motion previews stay independent');
    await page.click('nav [data-tab="broadcast"]');
    assert.equal(await page.locator('#l2dTestPreview').getAttribute('src'), null);
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

    await page.locator('#layoutRiceOn').check();
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    const riceBefore = {...cfg.rice_counter}, riceBox = await page.locator('#layoutRiceBox').boundingBox();
    await page.mouse.move(riceBox.x + riceBox.width / 2, riceBox.y + riceBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(riceBox.x + riceBox.width / 2 + 100, riceBox.y + riceBox.height / 2 - 45, {steps: 5});
    await page.mouse.up();
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(typeof cfg.rice_counter.overlay_x, 'number');
    assert.ok(cfg.rice_counter.overlay_y < 0.9);
    const riceHandle = await page.locator('#layoutRiceResize').boundingBox();
    await page.mouse.move(riceHandle.x + riceHandle.width / 2, riceHandle.y + riceHandle.height / 2);
    await page.mouse.down();
    await page.mouse.move(riceHandle.x + riceHandle.width / 2 + 35, riceHandle.y + riceHandle.height / 2 + 25, {steps: 4});
    await page.mouse.up();
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.ok(cfg.rice_counter.overlay_size > riceBefore.overlay_size);
    await page.locator('#layoutRiceX').evaluate(el => { el.value = '60'; el.dispatchEvent(new Event('input', {bubbles: true})); });
    await page.locator('#layoutRiceY').evaluate(el => { el.value = '30'; el.dispatchEvent(new Event('input', {bubbles: true})); });
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(cfg.rice_counter.overlay_x, 0.6);
    assert.equal(cfg.rice_counter.overlay_y, 0.3);
    const skinBeforeRiceReset = JSON.stringify(cfg.live2d);
    await page.click('#layoutRiceReset');
    await page.waitForFunction(() => document.querySelector('#layoutSaveState').textContent.includes('已保存'));
    assert.equal(cfg.rice_counter.overlay_size, 0.16);
    assert.equal(cfg.rice_counter.overlay_x, null);
    assert.equal(cfg.rice_counter.overlay_y, null);
    assert.equal(JSON.stringify(cfg.live2d), skinBeforeRiceReset);
    console.log('PASS rice drag, resize, sliders and reset persist independently of Live2D');

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
    assert.equal(await page.locator('#layoutRiceSize').inputValue(), '0.16');
    assert.deepEqual(errors, []);
    console.log('PASS saved settings survive reload; no JavaScript errors');
    await page.click('nav [data-tab="l2d"]');
    const switchStart = requests.length;
    await page.locator('[data-k="live2d.scale"]').evaluate(el => {
      el.value = '1.65'; el.dispatchEvent(new Event('input', {bubbles: true}));
    });
    await page.selectOption('#l2dLib', 'second');
    await page.waitForFunction(() => !document.querySelector('#l2dLib').disabled && document.querySelector('#l2dModelFile').value === 'second.model3.json');
    const switchRequests = requests.slice(switchStart);
    const saveIndex = switchRequests.findIndex(r => r.url === '/api/config' && r.body?.live2d?.scale === 1.65);
    const switchIndex = switchRequests.findIndex(r => r.url === '/api/l2d/switch');
    assert.ok(saveIndex >= 0 && saveIndex < switchIndex);
    assert.equal(await page.locator('#l2dLib').inputValue(), 'second');
    rejectSwitch = true;
    await page.selectOption('#l2dLib', 'fixture');
    await page.waitForFunction(() => !document.querySelector('#l2dLib').disabled);
    assert.equal(await page.locator('#l2dLib').inputValue(), 'second');
    assert.ok((await page.locator('#toast').textContent()).includes('fixture switch failed'));
    assert.deepEqual(errors, []);
    console.log('PASS pending model edits flush before switching; failed switches restore selection');
    await page.click('nav [data-tab="themes"]');
    await page.waitForFunction(() => document.querySelector('#themePreviewImage').complete);
    assert.equal(await page.locator('#themeCurrent').textContent(), '当前 · 原始画面');
    const originalLayout = {...cfg.live2d};
    const oceanButton = page.locator('[data-stream-preset="ocean"]');
    await oceanButton.click();
    await page.waitForFunction(() => document.querySelector('#themeSaveState').textContent.includes('主题已保存'));
    assert.equal(cfg.stream_theme.preset, 'ocean');
    assert.equal(await oceanButton.getAttribute('aria-pressed'), 'true');
    assert.ok(await page.locator('#themeOceanSettings').isVisible());
    assert.ok(await page.locator('#themeLiveImage').isVisible());
    assert.ok((await page.locator('#themeLiveImage').getAttribute('src')).includes('/api/broadcast/stream.mjpg'));
    assert.ok(await page.locator('#themeAvatarPreview').isHidden(), 'actual composite must not get a second avatar');
    await page.click('#themeOpenBroadcast');
    await page.click('#broadcastPreviewStop');
    await page.click('nav [data-tab="themes"]');
    await page.waitForFunction(() => document.querySelector('#themePreviewImage').complete && document.querySelector('#themePreviewImage').naturalWidth > 0);
    assert.ok(await page.locator('#themeAvatarPreview').isVisible());
    await page.fill('[data-k="stream_theme.title"]', '海盐汽水');
    await page.locator('[data-k="stream_theme.subtitle"]').focus();
    await page.waitForFunction(() => document.querySelector('#themeSaveState').textContent.includes('主题已保存'));
    assert.equal(cfg.stream_theme.title, '海盐汽水');
    await page.click('#themeOpenLayout');
    assert.equal(await page.locator('#layoutX').inputValue(), '12.5');
    assert.equal(await page.locator('#layoutSize').inputValue(), '0.54');
    assert.ok(await page.locator('#overlayCorner').isDisabled());
    assert.ok(await page.locator('#layoutThemeBackground').isVisible());
    await page.locator('#layoutSize').evaluate(el => { el.value = '0.62'; el.dispatchEvent(new Event('input', {bubbles: true})); });
    await page.waitForFunction(() => document.querySelector('#themeSaveState').textContent.includes('主题已保存'));
    assert.equal(cfg.stream_theme.avatar_size, 0.62);
    assert.deepEqual(cfg.live2d, originalLayout, 'ocean layout edits must preserve the original layout');
    await page.click('nav [data-tab="themes"]');
    await page.click('[data-stream-preset="classic"]');
    await page.waitForFunction(() => document.querySelector('#themeCurrent').textContent.includes('原始画面') && !document.querySelector('[data-stream-preset="classic"]').disabled);
    await page.click('#themeOpenLayout');
    assert.ok(await page.locator('#overlayCorner').isEnabled());
    assert.deepEqual(cfg.live2d, originalLayout);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#l2dModelFile').options.length === 1);
    await page.click('nav [data-tab="themes"]');
    await oceanButton.click();
    await page.waitForFunction(() => document.querySelector('#themeSaveState').textContent.includes('主题已保存'));
    assert.equal(await page.locator('[data-k="stream_theme.title"]').inputValue(), '海盐汽水');
    await page.click('#themeOpenLayout');
    assert.equal(await page.locator('#layoutSize').inputValue(), '0.62');
    await page.setViewportSize({width: 780, height: 1000});
    await page.click('nav [data-tab="themes"]');
    assert.ok(await page.locator('[data-stream-preset="ocean"]').isVisible());
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('PASS ocean theme switching, separate layout, saved copy, reload and narrow viewport');
    await page.setViewportSize({width: 1320, height: 1000});
    Object.assign(cfg.rice_counter, {overlay_x: 0.7, overlay_y: 0.3, overlay_size: 0.24});
    await page.click('nav [data-tab="broadcast"]');
    await page.locator('#broadcast [data-k="overlay.enabled"]').uncheck();
    await page.waitForFunction(() => pending.overlay?.enabled === false || Object.keys(pending).length === 0);
    await page.evaluate(() => flushConfig());
    assert.equal(cfg.overlay.enabled, false);
    assert.equal(await page.locator('#themes [data-k="overlay.enabled"]').isChecked(), false);
    assert.equal(await page.locator('#obs [data-k="overlay.enabled"]').isChecked(), false);
    await page.reload();
    await page.waitForFunction(() => CFG.overlay?.enabled === false);
    await page.click('nav [data-tab="obs"]');
    await page.locator('#obs [data-k="overlay.enabled"]').check();
    await page.evaluate(() => flushConfig());
    assert.equal(cfg.overlay.enabled, true);
    console.log('PASS subtitle switches sync across broadcast/theme/OBS settings and persist on reload');
    await page.click('nav [data-tab="voice"]');
    const bufferInput = page.locator('[data-k="tts.stream_buffer_seconds"]');
    assert.equal(await bufferInput.inputValue(), '0.8');
    await bufferInput.fill('1.2');
    await bufferInput.dispatchEvent('change');
    await page.evaluate(() => flushConfig());
    assert.equal(cfg.tts.stream_buffer_seconds, 1.2);
    await page.reload();
    await page.waitForFunction(() => CFG.tts?.stream_buffer_seconds === 1.2);
    await page.click('nav [data-tab="voice"]');
    assert.equal(await bufferInput.inputValue(), '1.2');
    assert.deepEqual(errors, []);
    console.log('PASS stream prebuffer setting is visible, saved and restored on reload');
    await page.goto('http://127.0.0.1:18787/rice');
    await page.waitForFunction(() => document.querySelector('canvas').style.left !== '');
    await page.waitForFunction(() => document.querySelector('canvas').style.height === '240px');
    const expected = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import json,sys; from live_rice import layout_rect; print(json.dumps(layout_rect(json.loads(sys.argv[1]),1320,1000)))', JSON.stringify(cfg.rice_counter)], {cwd: root, encoding: 'utf8'}));
    const rect = await page.locator('canvas').boundingBox();
    assert.deepEqual([rect.x, rect.y, rect.width, rect.height], expected);
    assert.deepEqual(errors, []);
    console.log('PASS transparent rice browser source matches native composite coordinates and size');
    console.log('Screenshot:', screenshot);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
