// 主动互动设置回归：全部接口拦截，配置仅保存在内存中。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
const interactionDefaults = structuredClone(cfg.interaction);
const errors = [], patches = [];

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
    await page.routeWebSocket('**/ws', ws => ws.onMessage(() => {}));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://127.0.0.1:18787') return route.fulfill({body: ''});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      let result = {};
      if (url.pathname === '/api/config') {
        if (request.method() === 'POST') {
          assert.equal(request.headers()['x-bvb'], '1');
          const patch = request.postDataJSON(); patches.push(patch); merge(cfg, patch); result = {ok: true};
        } else result = cfg;
      } else if (url.pathname === '/api/l2d/info') result = {cfg: cfg.live2d, expressions: [], motions: [], model_files: []};
      else if (url.pathname === '/api/tts/weights') result = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') result = [];
      else if (url.pathname === '/api/songs/state') result = {current: null, queue: [], credits: []};
      else if (url.pathname === '/api/broadcast/account') result = {ok: true, logged_in: false};
      else if (url.pathname === '/api/broadcast/sources') result = {ok: true, windows: [], audio: []};
      else if (url.pathname === '/api/broadcast/areas') result = {ok: true, areas: []};
      else if (url.pathname === '/api/broadcast/status') result = {running: false, previewing: false, phase: 'idle'};
      else if (url.pathname === '/api/broadcast/schedule') result = {ok: true, enabled: false, start_time: '20:00', stop_time: '23:00'};
      await route.fulfill({json: result});
    });
    const enabled = page.locator('[data-k="interaction.enabled"]');
    const idle = page.locator('[data-k="interaction.idle_seconds"]');
    const interval = page.locator('[data-k="interaction.interval_seconds"]');
    await page.goto('http://127.0.0.1:18787/');
    await page.locator('nav [data-tab="rules"]').click();
    await page.waitForFunction(() => document.querySelector('[data-k="interaction.idle_seconds"]').value === '20');
    assert.equal(await enabled.isChecked(), true);
    assert.equal(await interval.inputValue(), '60');
    const followEnabled = page.locator('[data-k="interaction.follow_enabled"]');
    const followInterval = page.locator('[data-k="interaction.follow_interval_seconds"]');
    const followTemplates = page.locator('[data-k="interaction.follow_templates"]');
    const followPrompt = page.locator('[data-k="interaction.follow_ai_prompt"]');
    assert.equal(await followEnabled.isChecked(), true);
    assert.equal(await followInterval.inputValue(), '180');
    assert.match(await followTemplates.inputValue(), /点个关注/);
    assert.match(await followPrompt.inputValue(), /求关注或求点赞/);
    await followInterval.fill('240'); await followInterval.blur();
    await followTemplates.fill('喜欢就点个关注呀！\n开心的话点点赞吧！'); await followTemplates.blur();
    await followPrompt.fill('结合游戏自然求关注，每次换个说法。'); await followPrompt.blur();
    await followEnabled.uncheck();
    assert.equal(await page.locator('[data-k="events.gift.tiered_thanks"]').isChecked(), true);
    const supporterWelcome = page.locator('[data-k="events.enter.supporter_welcome"]');
    const supporterTemplate = page.locator('[data-k="events.enter.supporter_templates"]');
    const supporterPrompt = page.locator('[data-k="events.enter.supporter_ai_prompt"]');
    assert.equal(await supporterWelcome.isChecked(), true);
    assert.match(await supporterTemplate.inputValue(), /你的心意我一直记着/);
    assert.match(await supporterPrompt.inputValue(), /以前在本直播间送过礼物/);
    await supporterTemplate.fill('欢迎{name}回来一起开心玩！'); await supporterTemplate.blur();
    await supporterPrompt.fill('亲切欢迎{name}，记住之前的心意。'); await supporterPrompt.blur();
    await supporterWelcome.uncheck();
    await idle.fill('35'); await idle.blur();
    await interval.fill('90'); await interval.blur();
    await enabled.uncheck();
    await page.evaluate(() => flushConfig());
    assert.deepEqual(cfg.interaction, {...interactionDefaults, enabled: false, idle_seconds: 35, interval_seconds: 90,
      follow_enabled: false, follow_interval_seconds: 240, follow_templates: '喜欢就点个关注呀！\n开心的话点点赞吧！',
      follow_ai_prompt: '结合游戏自然求关注，每次换个说法。'});
    assert.ok(patches.length);
    await page.reload();
    await page.locator('nav [data-tab="rules"]').click();
    await page.waitForFunction(() => document.querySelector('[data-k="interaction.idle_seconds"]').value === '35');
    assert.equal(await enabled.isChecked(), false);
    assert.equal(await interval.inputValue(), '90');
    assert.equal(await followEnabled.isChecked(), false);
    assert.equal(await followInterval.inputValue(), '240');
    assert.equal(await followTemplates.inputValue(), '喜欢就点个关注呀！\n开心的话点点赞吧！');
    assert.equal(await followPrompt.inputValue(), '结合游戏自然求关注，每次换个说法。');
    await followEnabled.check();
    assert.equal(await supporterWelcome.isChecked(), false);
    assert.equal(await supporterTemplate.inputValue(), '欢迎{name}回来一起开心玩！');
    assert.equal(await supporterPrompt.inputValue(), '亲切欢迎{name}，记住之前的心意。');
    await supporterWelcome.check();
    await page.evaluate(() => flushConfig());
    assert.equal(cfg.events.enter.supporter_welcome, true);
    assert.equal(cfg.interaction.follow_enabled, true);
    assert.equal(cfg.interaction.enabled, false);
    assert.deepEqual(errors, []);
    console.log('PASS 主动聊天、定时关注点赞及暖心欢迎：默认开启、独立开关、文案与间隔设置、自动保存及刷新保留');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
