// Edge 页面交互回归：接口和 WebSocket 均用本地测试数据，不读取真实账号或播放音频。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
const audio = path.join(root, 'logs/fixture-ai-cover.wav');
let liveSocket, mode = 'ok', tested = 0;
const errors = [], requests = [];
let state = {enabled: false, action: 'audio', busy: false, latest: null, last_result: null};
function merge(target, patch) {
  for (const [key, value] of Object.entries(patch)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) merge(target[key] ||= {}, value);
    else target[key] = value;
  }
}

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 1100}});
    page.on('pageerror', error => errors.push(error.message));
    await page.clock.install();
    await page.routeWebSocket('**/ws', socket => { liveSocket = socket; socket.onMessage(() => {}); });
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://127.0.0.1:18787') return route.fulfill({body: ''});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      const body = request.method() === 'POST' ? request.postDataJSON() : undefined;
      requests.push({path: url.pathname, body});
      if (body) assert.equal(request.headers()['x-bvb'], '1');
      let result = {};
      if (url.pathname === '/api/config') {
        if (body) { merge(cfg, body); state.enabled = cfg.wishlist.enabled; state.action = cfg.wishlist.action; result = {ok: true}; }
        else result = cfg;
      } else if (url.pathname === '/api/wishlist/state') result = state;
      else if (url.pathname === '/api/wishlist/test') {
        tested++;
        if (mode === 'error') result = {ok: false, error: '请选择有效的 AI 翻唱音频文件'};
        else {
          state.last_result = {ok: true, time: '12:00:00', message: 'AI 翻唱 / 本地音频已加入播报队列'};
          result = {ok: true, message: state.last_result.message};
        }
      } else if (url.pathname === '/api/pick') result = {path: audio};
      else if (url.pathname === '/api/l2d/info') result = {cfg: cfg.live2d, expressions: [], motions: [], model_files: []};
      else if (url.pathname === '/api/l2d/library') result = {models: [], current: ''};
      else if (url.pathname === '/api/tts/weights') result = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') result = [];
      else if (url.pathname === '/api/songs/state') result = {current: null, queue: [], credits: []};
      else if (url.pathname === '/api/broadcast/account') result = {ok: true, logged_in: false};
      else if (url.pathname === '/api/broadcast/sources') result = {ok: true, windows: [], audio: []};
      else if (url.pathname === '/api/broadcast/areas') result = {ok: true, areas: []};
      else if (url.pathname === '/api/broadcast/status') result = {running: false, previewing: false, phase: 'idle'};
      else if (url.pathname === '/api/broadcast/schedule') result = {enabled: false, start_time: '20:00', stop_time: '23:00'};
      await route.fulfill({contentType: 'application/json', body: JSON.stringify(result)});
    });
    await page.goto('http://127.0.0.1:18787/');
    await page.locator('nav [data-tab="songs"]').click();
    assert.equal(await page.locator('[data-wish-action="audio"]').isVisible(), true);
    for (const action of ['song', 'say', 'ai', 'audio']) {
      await page.locator('#wishAction').selectOption(action);
      assert.equal(await page.locator(`[data-wish-action="${action}"]`).isVisible(), true);
      assert.equal(await page.locator('[data-wish-action]:visible').count(), 1);
    }
    console.log('PASS 实际页面切换四种动作，参数区域正确');
    await page.locator('[data-pick="wishlist.audio_file"]').click();
    await page.waitForFunction(audio => document.querySelector('[data-k="wishlist.audio_file"]').value === audio, audio);
    await page.locator('#wishTest').click();
    await page.waitForFunction(() => document.querySelector('#wishResult').textContent.includes('加入播报队列'));
    assert.equal(cfg.wishlist.audio_file, audio);
    const lastTest = requests.findLastIndex(request => request.path === '/api/wishlist/test');
    const lastSave = requests.findLastIndex(request => request.path === '/api/config' && request.body);
    assert.ok(lastSave < lastTest);
    assert.equal(tested, 1);
    console.log('PASS 选择翻唱文件，先保存再测试');
    await page.locator('[data-k="wishlist.enabled"]').check();
    await page.clock.runFor(600);
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('设置已保存'));
    assert.equal(cfg.wishlist.enabled, true);
    state.latest = {wish_name: '今日心愿', status: 2, items: [{name: '小花花', current: 3, target: 3}]};
    liveSocket.send(JSON.stringify({kind: 'wishlist', ...state}));
    await page.waitForFunction(() => document.querySelector('#wishStatus').textContent.includes('今日心愿 · 已达成'));
    assert.ok((await page.locator('#wishItems').textContent()).includes('小花花 已达成'));
    console.log('PASS 开关保存与心愿单达成状态实时更新');
    mode = 'error';
    await page.locator('#wishTest').click();
    await page.waitForFunction(() => document.querySelector('#wishResult').textContent.includes('请选择有效'));
    assert.equal(await page.locator('#wishTest').isEnabled(), true);
    console.log('PASS 测试失败显示具体错误，按钮可以重试');
    mode = 'ok';
    await page.reload();
    await page.locator('nav [data-tab="songs"]').click();
    await page.waitForFunction(audio => document.querySelector('[data-k="wishlist.audio_file"]').value === audio, audio);
    assert.equal(await page.locator('[data-k="wishlist.enabled"]').isChecked(), true);
    await page.clock.runFor(3100);
    await page.waitForFunction(() => document.querySelector('#wishStatus').textContent.includes('今日心愿'));
    await page.screenshot({path: path.join(root, 'logs/wishlist-actions-preview.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log('PASS 刷新后保留设置，页面无未捕获异常，已保存预览截图');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
