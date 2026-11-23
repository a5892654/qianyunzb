// 播放类型与离线模拟操作。全部服务请求由本地替身响应，不连接 B 站。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright-core');
const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
const output = path.join(root, 'outputs/wage-preview');
fs.mkdirSync(output, {recursive: true});
execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', "import cv2; from live_wage import WageRenderer,demo_state; cv2.imwrite('outputs/wage-preview/working.png', WageRenderer().render(demo_state()))"], {cwd: root});
const errors = [], events = [], previews = [], soundTests = [];
let running = false, previewing = false, slackPlay = null;
const merge = (a, b) => { for (const [k, v] of Object.entries(b)) { if (v && typeof v === 'object' && !Array.isArray(v)) merge(a[k] ||= {}, v); else a[k] = v; } };
(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    page.on('pageerror', e => errors.push(e.message));
    await page.routeWebSocket('**/ws', ws => ws.onMessage(() => {}));
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== 'http://127.0.0.1:18787') return route.abort();
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      if (url.pathname.endsWith('.mjpg')) return route.fulfill({contentType: 'image/png', body: fs.readFileSync(path.join(output, 'working.png'))});
      const body = req.method() === 'POST' ? req.postDataJSON() : null;
      let result = {ok: true};
      if (url.pathname === '/api/config') {
        if (body) merge(cfg, body);
        else result = cfg;
      } else if (url.pathname === '/api/l2d/info') result = {cfg: cfg.live2d, expressions: [], motions: [], model_files: []};
      else if (url.pathname === '/api/l2d/library') result = {models: []};
      else if (url.pathname === '/api/tts/weights') result = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') result = [];
      else if (url.pathname === '/api/songs/state') result = {queue: [], credits: []};
      else if (url.pathname === '/api/broadcast/account') result = {ok: true, logged_in: false};
      else if (url.pathname === '/api/broadcast/sources') result = {ok: true, windows: [], audio: [{id: 7, name: '离线测试设备'}], default_audio: 7};
      else if (url.pathname === '/api/broadcast/status') result = {running: false, previewing, phase: previewing ? 'preview' : 'idle'};
      else if (url.pathname === '/api/broadcast/schedule') result = {enabled: false, plans: [], livehime: {}, external_timer: {}};
      else if (url.pathname === '/api/dtgame/status') result = {running, game_kind: cfg.game.active, game_text: '上班中', ready: true, wage: {slack_play: slackPlay}, game: {}};
      else if (url.pathname === '/api/dtgame/resume') running = true;
      else if (url.pathname === '/api/dtgame/stop') running = false;
      else if (url.pathname === '/api/test_event') events.push(body);
      else if (url.pathname === '/api/wage/sfx/test') soundTests.push(body);
      else if (url.pathname === '/api/broadcast/preview') { previews.push(body); previewing = true; }
      return route.fulfill({contentType: 'application/json', body: JSON.stringify(result)});
    });
    await page.goto('http://127.0.0.1:18787');
    await page.waitForFunction(() => [...document.querySelector('#broadcastGameSelect').options].some(option => option.value === 'wage'));
    await page.selectOption('#broadcastGameSelect', 'wage');
    await page.waitForFunction(() => document.querySelector('#broadcastProgramHint').style.display !== 'none');
    await page.waitForFunction(() => document.querySelector('#gameActive').value === 'wage');
    assert.equal(await page.locator('#broadcastWindowRow').isVisible(), false);
    assert.equal(await page.locator('#broadcastSharkSoundRow').isVisible(), true);
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/config' && r.request().postDataJSON()?.wage?.boss_sfx_enabled === false),
      page.locator('#broadcastSharkSound').uncheck()
    ]);
    assert.equal(cfg.wage.boss_sfx_enabled, false);
    assert.equal(await page.locator('#wageSharkSound').isChecked(), false);
    assert.equal(await page.locator('#wageSfxTest').isDisabled(), true);
    assert.match(await page.locator('#broadcastSharkSoundState').textContent(), /已关闭/);
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/broadcast/preview' && r.request().method() === 'POST'),
      page.click('#broadcastPreview')
    ]);
    await page.waitForFunction(() => document.querySelector('#broadcastAudio').value === '7');
    await page.waitForFunction(() => document.querySelector('#broadcastPreview').textContent === '预览画面和声音');
    assert.deepEqual(previews, [{hwnd: 0, audio_device: 7}]);
    assert.equal(await page.locator('#broadcastError').textContent(), '');
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/dtgame/resume'),
      page.click('#broadcastDtStart')
    ]);
    await page.waitForFunction(() => document.querySelector('#broadcastDtStart').disabled);
    assert.equal(running, true);
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/dtgame/stop'),
      page.click('#broadcastDtStop')
    ]);
    await page.waitForFunction(() => document.querySelector('#broadcastDtStart').disabled === false);
    assert.equal(running, false);
    await page.locator('nav button[data-tab="ai"]').click();
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/config' && r.request().postDataJSON()?.wage?.boss_sfx_enabled === true),
      page.locator('#wageSharkSound').check()
    ]);
    assert.equal(cfg.wage.boss_sfx_enabled, true);
    assert.equal(await page.locator('#broadcastSharkSound').isChecked(), true);
    assert.equal(await page.locator('#wageSfxTest').isDisabled(), false);
    await page.locator('[data-k="wage.avatar_zoom"]').fill('1');
    await page.locator('[data-k="wage.avatar_zoom"]').dispatchEvent('change');
    await page.click('#wageOfficeFraming');
    await page.waitForFunction(() => document.querySelector('[data-k="wage.avatar_zoom"]').value === '1.8');
    await page.locator('[data-k="wage.work_reward"]').fill('150');
    await page.locator('[data-k="wage.work_reward"]').dispatchEvent('change');
    for (const id of ['wageTestSlack', 'wageTestCatch', 'wageTestGift', 'wageTestPoll']) {
      await Promise.all([
        page.waitForResponse(r => new URL(r.url()).pathname === '/api/test_event'),
        page.click('#' + id)
      ]);
    }
    assert.equal(cfg.wage.work_reward, 150);
    assert.equal(cfg.wage.avatar_zoom, 1.8);
    assert.equal(cfg.wage.avatar_y, 0.67);
    assert.deepEqual(events.map(e => e.text || e.type), ['摸鱼', '老板来了', 'gift', '加薪投票']);
    await page.locator('[data-k="wage.task_seconds"]').fill('25');
    await page.locator('[data-k="wage.task_seconds"]').dispatchEvent('change');
    await page.locator('[data-k="wage.rescue_enabled"]').uncheck();
    for (const kind of ['rescue', 'task', 'rules', 'coffee']) {
      const selector = `[data-office-test="${kind}"]`;
      await page.click(selector);
      await page.waitForFunction(selector => !document.querySelector(selector).disabled, selector);
    }
    assert.equal(cfg.wage.task_seconds, 25);
    assert.equal(cfg.wage.rescue_enabled, false);
    await page.locator('[data-k="wage.boss_sfx_volume"]').fill('38');
    await page.locator('[data-k="wage.boss_sfx_volume"]').dispatchEvent('change');
    await page.selectOption('#wageSfxSound', 'scold');
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/wage/sfx/test'),
      page.click('#wageSfxTest')
    ]);
    assert.equal(cfg.wage.boss_sfx_volume, 38);
    assert.deepEqual(soundTests, [{sound: 'scold'}]);
    assert.deepEqual(events.slice(4).map(e => e.text), ['摸鱼', '老板来了', '鲨总派活', '改员工守则', '兑换咖啡', '使用咖啡']);
    assert.equal(await page.locator('#wageSlackPick option').count(), 12);
    await page.selectOption('#wageSlackPick', '偷吃零食');
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/test_event'),
      page.click('#wageTestSlack')
    ]);
    assert.equal(events.at(-1).text, '偷吃零食');
    for (const scenario of ['工位撸猫', '冲咖啡', '抽屉寻宝']) {
      await page.selectOption('#wageSlackPick', scenario);
      await Promise.all([
        page.waitForResponse(r => new URL(r.url()).pathname === '/api/test_event'),
        page.click('#wageTestSlack')
      ]);
      assert.equal(events.at(-1).text, scenario);
    }
    // 挑战操作选择器：默认「提竿」，模拟固定同一观众身份（uid 0 / 测试观众）
    assert.equal(await page.locator('#wageArcadeAction').inputValue(), '提竿');
    await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/test_event'),
      page.click('#wageTestArcade')
    ]);
    assert.deepEqual(events.at(-1), {type: 'danmaku', uid: 0, name: '测试观众', text: '提竿'});
    for (const action of ['摸摸', '加奶', '加糖', '加冰', '加浓', '抽屉1', '抽屉2', '抽屉3', '摸鱼图鉴']) {
      await page.selectOption('#wageArcadeAction', action);
      await Promise.all([
        page.waitForResponse(r => new URL(r.url()).pathname === '/api/test_event'),
        page.click('#wageTestArcade')
      ]);
      assert.equal(events.at(-1).text, action);
      assert.equal(events.at(-1).uid, 0);
      assert.equal(events.at(-1).name, '测试观众');
    }
    // 进度段：空快照时显示简短帮助
    slackPlay = null;
    await page.evaluate(() => pollDt());
    assert.match(await page.locator('#wageArcadeState').textContent(), /暂无进行中的挑战.*摸鱼图鉴/);
    // 进行中：当前指令、进度、投票计数与图鉴收集
    slackPlay = {active: {kind: 'treasure', title: '抽屉寻宝', hint: '发「抽屉1 / 抽屉2 / 抽屉3」投票，每人一票可改票', feedback: '已记下你的票', progress: 2, goal: 3, phase: '投票', choices: ['抽屉1', '抽屉2', '抽屉3'], counts: [1, 1, 0], left: 14}, result: null,
      book: {unlocked: ['看番', '偷吃零食', '摸鱼钓鱼', '工位撸猫'], total: 11, fish: ['鲫鱼', '锦鲤'], fish_total: 3, wins: 4, streak: 2, best_streak: 5}};
    await page.evaluate(() => pollDt());
    const arcadeText = await page.locator('#wageArcadeState').textContent();
    assert.match(arcadeText, /抽屉寻宝 · 投票/);
    assert.doesNotMatch(arcadeText, /进度/);
    assert.match(arcadeText, /抽屉1×1 抽屉2×1 抽屉3×0/);
    assert.match(arcadeText, /反馈：已记下你的票/);
    assert.match(arcadeText, /图鉴 4\/11/);
    assert.match(arcadeText, /鲫鱼、锦鲤\/3/);
    assert.match(arcadeText, /胜场 4 · 连胜 2 · 最高 5/);
    // 结算态：失败与成功文案都只走 textContent
    slackPlay = {active: null, result: {title: '冲咖啡', text: '配方顺序错了，咖啡洒了一桌', left: 0, success: false}, book: slackPlay.book};
    await page.evaluate(() => pollDt());
    assert.match(await page.locator('#wageArcadeState').textContent(), /冲咖啡：配方顺序错了，咖啡洒了一桌（失败）/);
    slackPlay = {active: null, result: {title: '工位撸猫', text: '猫猫被摸得很开心', left: 0, success: true}, book: slackPlay.book};
    await page.evaluate(() => pollDt());
    assert.match(await page.locator('#wageArcadeState').textContent(), /（安全完成）/);
    slackPlay = null;
    await page.evaluate(() => pollDt());
    await page.screenshot({path: path.join(output, 'settings.png'), fullPage: true});
    await page.locator('nav button[data-tab="broadcast"]').click();
    await page.screenshot({path: path.join(output, 'broadcast-ui.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log('PASS: playback selection, audio-only preview, start/pause, office settings, twelve slack scenarios, arcade action simulations, slack-play progress and book display, shark SFX audition; no page errors');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
