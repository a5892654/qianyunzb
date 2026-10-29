// 手动下播界面回归：拦截全部请求，不接触真实直播和配置。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
let running = false, finishing = false, finishCount = 0, complete;
const errors = [];

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1320, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    await page.routeWebSocket('**/ws', ws => ws.onMessage(() => {}));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://127.0.0.1:18787') return route.fulfill({body: ''});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      let result = {};
      if (url.pathname === '/api/config') result = cfg;
      else if (url.pathname === '/api/l2d/info') result = {cfg: cfg.live2d, expressions: [], motions: [], model_files: []};
      else if (url.pathname === '/api/tts/weights') result = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') result = [];
      else if (url.pathname === '/api/songs/state') result = {current: null, queue: [], credits: []};
      else if (url.pathname === '/api/broadcast/account') result = {ok: true, logged_in: true, name: '测试主播', room_id: 42};
      else if (url.pathname === '/api/broadcast/sources') result = {ok: true, windows: [], audio: []};
      else if (url.pathname === '/api/broadcast/areas') result = {ok: true, areas: []};
      else if (url.pathname === '/api/broadcast/status') result = {running, finishing, previewing: false, phase: running ? 'live' : 'idle', seconds: 3600};
      else if (url.pathname === '/api/broadcast/schedule') result = {ok: true, enabled: false, active_action: finishing ? 'farewell' : '', start_time: '20:00', stop_time: '23:00'};
      else if (url.pathname === '/api/broadcast/finish') {
        assert.equal(request.method(), 'POST');
        assert.equal(request.headers()['x-bvb'], '1');
        finishCount++;
        finishing = true;
        return new Promise(resolve => {
          complete = async response => {
            finishing = false;
            if (response.ok) running = false;
            await route.fulfill({json: response});
            resolve();
          };
        });
      }
      await route.fulfill({json: result});
    });
    await page.goto('http://127.0.0.1:18787/');
    await page.waitForFunction(() => document.querySelector('#broadcastAccount').textContent.includes('测试主播'));
    for (const id of ['broadcastFinish', 'broadcastDashFinish']) assert.equal(await page.locator('#' + id).isDisabled(), true);

    running = true;
    await page.evaluate(() => pollBroadcast());
    assert.equal(await page.locator('#broadcastFinish').isEnabled(), true);
    await page.locator('#broadcastFinish').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('正在告别'));
    await page.evaluate(() => pollBroadcast());
    await page.evaluate(() => pollSchedule());
    assert.match(await page.locator('#scheduleResult').textContent(), /正在告别/);
    for (const id of ['broadcastFinish', 'broadcastDashFinish']) {
      assert.equal(await page.locator('#' + id).isDisabled(), true);
      assert.equal(await page.locator('#' + id).textContent(), '正在告别…');
    }
    await page.evaluate(() => document.querySelector('#broadcastFinish').click());
    assert.equal(finishCount, 1);
    await complete({ok: true});
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('直播已停止'));
    await page.evaluate(() => pollSchedule());
    assert.equal(await page.locator('#scheduleResult').textContent(), '');
    await page.waitForFunction(() => document.querySelector('#broadcastFinish').textContent === '手动下播');
    assert.equal(await page.locator('#broadcastFinish').isDisabled(), true);

    running = true;
    await page.evaluate(() => pollSchedule());
    await page.evaluate(() => pollBroadcast());
    await page.locator('nav [data-tab="dash"]').click();
    await page.locator('#broadcastDashFinish').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('正在告别'));
    assert.equal(finishCount, 2);
    await complete({ok: false, error: '告别语音等待超时，直播尚未停止'});
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('等待超时'));
    await page.waitForFunction(() => !document.querySelector('#broadcastDashFinish').disabled);
    assert.deepEqual(errors, []);
    console.log('PASS 未开播禁用、两个入口、告别进度、防重复点击、成功停播、失败恢复');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
