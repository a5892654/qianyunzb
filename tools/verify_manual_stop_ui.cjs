// 手动下播界面回归：拦截全部请求，不接触真实直播和配置。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
let running = false, finishing = false, finishCount = 0, complete;
let timerStarts = 0, timerCancels = 0, timerFailure = '';
const schedule = {ok: true, enabled: false, mode: 'managed', start_time: '20:00', stop_time: '23:00',
  local_time: '2026-10-30 20:00', external_timer: {enabled: false}};
const scheduleSaves = [], confirmations = [];
const errors = [];

function merge(target, patch) {
  for (const [key, value] of Object.entries(patch)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) merge(target[key] ||= {}, value);
    else target[key] = value;
  }
}

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1320, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', async dialog => { confirmations.push(dialog.message()); await dialog.accept(); });
    await page.routeWebSocket('**/ws', ws => ws.onMessage(() => {}));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://127.0.0.1:18787') return route.fulfill({body: ''});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      let result = {};
      if (url.pathname === '/api/config') {
        if (request.method() === 'POST') { merge(cfg, request.postDataJSON()); result = {ok: true}; }
        else result = cfg;
      }
      else if (url.pathname === '/api/l2d/info') result = {cfg: cfg.live2d, expressions: [], motions: [], model_files: []};
      else if (url.pathname === '/api/tts/weights') result = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') result = [];
      else if (url.pathname === '/api/songs/state') result = {current: null, queue: [], credits: []};
      else if (url.pathname === '/api/broadcast/account') result = {ok: true, logged_in: true, name: '测试主播', room_id: 42};
      else if (url.pathname === '/api/broadcast/sources') result = {ok: true, windows: [], audio: []};
      else if (url.pathname === '/api/broadcast/areas') result = {ok: true, areas: []};
      else if (url.pathname === '/api/broadcast/status') result = {running, finishing, previewing: false, phase: running ? 'live' : 'idle', seconds: 3600};
      else if (url.pathname === '/api/broadcast/schedule') {
        if (request.method() === 'POST') {
          assert.equal(request.headers()['x-bvb'], '1');
          const data = request.postDataJSON(); scheduleSaves.push(data);
          Object.assign(schedule, data, {external_room_id: 42, next_stop: '2026-10-30 23:00'});
        }
        result = {...schedule, active_action: finishing ? 'farewell' : ''};
      } else if (url.pathname === '/api/broadcast/timer/start') {
        assert.equal(request.headers()['x-bvb'], '1');
        assert.equal(request.postDataJSON().minutes, 5);
        timerStarts++;
        if (timerFailure) result = {ok: false, error: timerFailure};
        else {
          schedule.external_timer = {enabled: true, room_id: 42, deadline: '2026-10-30 20:05:00', remaining_seconds: 300};
          result = schedule;
        }
      } else if (url.pathname === '/api/broadcast/timer/cancel') {
        assert.equal(request.headers()['x-bvb'], '1');
        timerCancels++; schedule.external_timer = {enabled: false}; result = schedule;
      }
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

    running = false;  // 直播姬正在推流，本工具的本地推流状态仍为 false。
    await page.locator('nav [data-tab="broadcast"]').click();
    await page.evaluate(() => pollBroadcast());
    await page.locator('[data-k="broadcast.auto_stop_minutes"]').fill('5');
    await page.locator('[data-k="broadcast.auto_stop_minutes"]').blur();
    await page.locator('#externalTimerStart').click();
    await page.waitForFunction(() => document.querySelector('#externalTimerStatus').textContent.includes('剩余 5 分钟'));
    assert.equal(timerStarts, 1);
    assert.equal(cfg.broadcast.auto_stop_minutes, 5);
    assert.equal(await page.locator('#externalTimerCancel').isEnabled(), true);
    await page.locator('#externalTimerCancel').click();
    await page.waitForFunction(() => document.querySelector('#externalTimerStatus').textContent.includes('未启动'));
    assert.equal(timerCancels, 1);
    assert.equal(await page.locator('#externalTimerCancel').isDisabled(), true);
    timerFailure = '当前登录主播的直播间尚未开播';
    await page.locator('#externalTimerStart').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('尚未开播'));
    await page.waitForFunction(() => !document.querySelector('#externalTimerStart').disabled);
    assert.equal(await page.locator('#externalTimerStart').isEnabled(), true);

    await page.locator('#scheduleMode').selectOption('stop_only');
    assert.equal(await page.locator('#scheduleStartTime').isDisabled(), true);
    await page.locator('#scheduleEnabled').check();
    await page.locator('#scheduleStopTime').fill('23:30');
    await page.locator('#scheduleSave').click();
    await page.waitForFunction(() => document.querySelector('#scheduleStatus').textContent.includes('每日 23:30 停播'));
    assert.equal(scheduleSaves.at(-1).mode, 'stop_only');
    assert.equal(scheduleSaves.at(-1).authorized, true);
    assert.equal('hwnd' in scheduleSaves.at(-1), false);
    assert.equal('title' in scheduleSaves.at(-1), false);
    assert.match(confirmations.at(-1), /不会自动开播/);
    await page.locator('#scheduleDisable').click();
    await page.waitForFunction(() => document.querySelector('#scheduleStatus').textContent === '定时直播未启用。');

    await page.locator('nav [data-tab="songs"]').click();
    const requireGift = page.locator('[data-k="songs.require_gift"]');
    assert.equal(await requireGift.isChecked(), false);
    assert.match(await page.locator('#songs').textContent(), /点歌\+晴天/);
    await requireGift.check();
    await page.evaluate(() => flushConfig());
    assert.equal(cfg.songs.require_gift, true);
    await page.reload();
    await page.locator('nav [data-tab="songs"]').click();
    await page.waitForFunction(() => document.querySelector('[data-k="songs.require_gift"]').checked);
    assert.deepEqual(errors, []);
    console.log('PASS 手动下播、直播姬倒计时启停和错误恢复、仅定时停播免预览、免费点歌默认与送礼限制保存');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
