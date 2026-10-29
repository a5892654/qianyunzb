// 网易云登录浏览器回归：拦截所有接口，不读取真实配置或连接直播平台。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
const account = {nickname: '扫码测试用户', vip: 1};
let qrMode = 'normal', pollMode = 'normal', sequence = 0, pollCount = 0;
let replies = [], loggedIn = false, pendingPoll = null, delayedReady = null;
const errors = [];
const pollBodies = [];

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1320, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.clock.install();
    await page.addInitScript(() => {
      window.createNEFingerprint = () => ({getToken: async () => ({token: 'fixture-device-token'})});
      window.__fakeInitCaptcha = (options, ready, failed) => {
        window.__captchaOptions = options;
        if (window.__captchaMode === 'init_error') { failed(); return; }
        ready({destroy: () => { window.__captchaDestroyed = (window.__captchaDestroyed || 0) + 1; },
               popUp: () => { window.__captchaOpened = true; }});
      };
      window.initNECaptcha = window.__fakeInitCaptcha;
    });
    await page.routeWebSocket('**/ws', ws => ws.onMessage(() => {}));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://127.0.0.1:18787') return route.fulfill({body: ''});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      if (url.pathname === '/songs') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/songs.html'), 'utf8')});
      const body = request.method() === 'POST' ? request.postDataJSON() : undefined;
      let result = {};
      if (url.pathname.startsWith('/api/songs/') && body) assert.equal(request.headers()['x-bvb'], '1');
      if (url.pathname === '/api/config') result = {...cfg, songs: {...cfg.songs, _ne_logged_in: loggedIn}};
      else if (url.pathname === '/api/l2d/info') result = {cfg: cfg.live2d, expressions: [], motions: [], model_files: []};
      else if (url.pathname === '/api/tts/weights') result = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') result = [];
      else if (url.pathname === '/api/songs/state') result = {current: null, queue: [], credits: [], account: loggedIn ? account : null};
      else if (url.pathname === '/api/broadcast/account') result = {ok: true, logged_in: false};
      else if (url.pathname === '/api/broadcast/sources') result = {ok: true, windows: [], audio: []};
      else if (url.pathname === '/api/broadcast/areas') result = {ok: true, areas: []};
      else if (url.pathname === '/api/broadcast/status') result = {running: false, previewing: false, phase: 'idle'};
      else if (url.pathname === '/api/broadcast/schedule') result = {enabled: false, start_time: '20:00', stop_time: '23:00'};
      else if (url.pathname === '/api/songs/qr') {
        if (qrMode === 'html_error') return route.fulfill({status: 500, contentType: 'text/html', body: 'upstream failed'});
        if (qrMode === 'json_error') result = {ok: false, error: '连接网易云超时，请检查网络后重试'};
        else {
          const key = 'key-' + ++sequence;
          result = {ok: true, key, captcha_id: 'fixture-captcha-id', device_app_id: 'fixture-device-app', svg: `<svg xmlns="http://www.w3.org/2000/svg" data-key="${key}" viewBox="0 0 180 180"><rect width="180" height="180" fill="white"/><path d="M10 10h40v40H10z M130 10h40v40h-40z M10 130h40v40H10z"/></svg>`};
        }
      } else if (url.pathname === '/api/songs/qrpoll') {
        pollCount++;
        pollBodies.push(body);
        if (pollMode === 'delayed') return new Promise(resolve => {
          pendingPoll = async result => {
            await route.fulfill({contentType: 'application/json', body: JSON.stringify(result)}).catch(() => {});
            pendingPoll = null; resolve();
          };
          delayedReady?.(); delayedReady = null;
        });
        result = replies.shift() || {ok: true, code: 801, msg: '等待扫码'};
        if (result.code === 803) loggedIn = true;
      } else if (url.pathname === '/api/songs/cookie') {
        loggedIn = !!body.cookie;
        result = {ok: true, account: loggedIn ? account : null};
      }
      await route.fulfill({contentType: 'application/json', body: JSON.stringify(result)});
    });
    await page.goto('http://127.0.0.1:18787/');
    await page.locator('nav [data-tab="songs"]').click();
    const message = page.locator('#neQrMsg');
    const expectMessage = text => page.waitForFunction(text => document.querySelector('#neQrMsg').textContent.includes(text), text);
    async function start() {
      const before = sequence;
      await page.locator('#neQrBtn').click();
      await page.waitForFunction(key => document.querySelector('#neQrSvg svg')?.dataset.key === key, 'key-' + (before + 1));
    }
    async function poll(text, ms = 2000) {
      await page.clock.runFor(ms);
      await expectMessage(text);
    }

    qrMode = 'html_error';
    await page.locator('#neQrBtn').click();
    await expectMessage('HTTP 500');
    qrMode = 'json_error';
    await page.locator('#neQrBtn').click();
    await expectMessage('连接网易云超时');
    console.log('PASS 二维码生成失败与非 JSON 响应可见');

    qrMode = 'normal';
    replies = [{ok: true, code: 8821, msg: '网易云要求安全验证，请点击“开始安全验证”后继续登录'}, {ok: true, code: 803, msg: '登录成功', account}];
    await start();
    const securityKey = 'key-' + sequence;
    await poll('开始安全验证', 1000);
    assert.equal(await page.locator('#neVerifyBtn').isVisible(), true);
    const pausedCount = pollCount;
    await page.clock.runFor(10000);
    assert.equal(pollCount, pausedCount, '等待安全验证时必须暂停轮询');
    await page.screenshot({path: path.join(root, 'logs/netease-8821-verification.png'), fullPage: true});
    await page.locator('#neVerifyBtn').click();
    await page.waitForFunction(() => window.__captchaOpened);
    await page.evaluate(() => { window.__verifiedCallback = window.__captchaOptions.onVerify; window.__verifiedCallback(null, {validate: 'fixture-captcha-proof'}); });
    await expectMessage('登录成功');
    await page.waitForFunction(() => document.querySelector('#neState').textContent.includes('扫码测试用户'));
    assert.equal(pollBodies.at(-1).key, securityKey);
    assert.equal(pollBodies.at(-1).secure_captcha, 'fixture-captcha-proof');
    assert.equal(pollBodies.at(-1).yd_device_token, 'fixture-device-token');
    const verifiedCount = pollCount;
    await page.evaluate(() => window.__verifiedCallback(null, {validate: 'fixture-captcha-proof'}));
    await page.clock.runFor(1000);
    assert.equal(pollCount, verifiedCount, '同一个验证结果只能提交一次');
    console.log('PASS 8821 暂停轮询，完成官方验证后用同一个二维码登录，验证结果只提交一次');

    replies = [{ok: true, code: 8821, msg: '请完成安全验证'}];
    await start(); await poll('安全验证', 1000);
    await page.locator('#neVerifyBtn').click();
    await page.waitForFunction(() => !!window.__captchaOptions);
    await page.evaluate(() => window.__captchaOptions.onClose());
    await expectMessage('已关闭');
    assert.equal(await page.locator('#neVerifyBtn').isEnabled(), true);
    await page.evaluate(() => { window.__captchaMode = 'init_error'; });
    await page.locator('#neVerifyBtn').click(); await expectMessage('初始化失败');
    await page.evaluate(() => { window.__captchaMode = ''; });
    await page.locator('#neVerifyBtn').click();
    await expectMessage('请完成网易安全验证');
    await page.evaluate(() => { window.__staleCallback = window.__captchaOptions.onVerify; });
    await start();
    const beforeStale = pollCount;
    await page.evaluate(() => window.__staleCallback(null, {validate: 'stale-proof'}));
    assert.equal(pollCount, beforeStale);
    assert.ok(!(await message.textContent()).includes('验证完成'));
    console.log('PASS 关闭或初始化失败可重试，更换二维码后旧验证回调失效');

    replies = [{ok: true, code: 8821, msg: '请完成安全验证'}];
    await start(); await poll('安全验证', 1000);
    await page.evaluate(() => { delete window.initNECaptcha; });
    await page.locator('#neVerifyBtn').click(); await expectMessage('加载失败');
    assert.equal(await page.locator('#neVerifyBtn').isEnabled(), true);
    await page.evaluate(() => { window.initNECaptcha = window.__fakeInitCaptcha; });
    console.log('PASS 官方验证脚本加载失败有提示并可重新加载');

    replies = [{ok: true, code: 8830, msg: '网易云要求进一步账号验证，请在官网完成登录后使用手动 Cookie 登录'}];
    await start(); await poll('进一步账号验证', 1000);
    const terminalCount = pollCount;
    await page.clock.runFor(6000);
    assert.equal(pollCount, terminalCount);
    assert.equal(await page.locator('#neVerifyBtn').isVisible(), false);
    assert.equal(await page.locator('#neCookie').isVisible(), true);
    console.log('PASS 8830 停止轮询并显示官网登录后的手动登录入口');

    replies = [{ok: true, code: 801, msg: '等待扫码'}, {ok: true, code: '802', msg: '已扫码，请在手机上确认'}, {ok: true, code: 803, msg: '登录成功', account}];
    await start();
    await poll('等待扫码', 1000);
    await poll('已扫码');
    await poll('登录成功');
    await page.waitForFunction(() => document.querySelector('#neState').textContent.includes('扫码测试用户'));
    console.log('PASS 等待扫码 → 手机确认 → 登录成功并立即显示账号');

    pollMode = 'delayed';
    await start();
    const beforeDelay = pollCount;
    const delayedRequest = new Promise(resolve => { delayedReady = resolve; });
    await page.clock.runFor(1000);
    await delayedRequest;
    assert.ok(pendingPoll);
    await page.clock.runFor(10000);
    assert.equal(pollCount, beforeDelay + 1, '慢请求期间不能重复发送轮询');
    const releaseOld = pendingPoll;
    await start();
    await releaseOld({ok: true, code: 803, msg: '旧二维码已登录', account: {nickname: '旧账号'}});
    await expectMessage('请用网易云音乐');
    assert.ok(!(await message.textContent()).includes('旧二维码'));
    console.log('PASS 慢请求不重叠，旧扫码响应不能覆盖新二维码');

    pollMode = 'normal';
    await page.locator('#neLogout').click();
    await page.waitForFunction(() => document.querySelector('#neState').textContent.includes('未登录'));
    const beforeLogout = pollCount;
    await page.clock.runFor(6000);
    assert.equal(pollCount, beforeLogout);
    assert.equal(await page.locator('#neQrBox').isVisible(), false);
    console.log('PASS 退出登录停止轮询并刷新账号状态');

    pollMode = 'delayed';
    await start();
    const timeoutRequest = new Promise(resolve => { delayedReady = resolve; });
    await page.clock.runFor(1000);
    await timeoutRequest;
    const releaseTimeout = pendingPoll;
    await page.clock.fastForward(40001);
    await expectMessage('连接超时');
    await releaseTimeout({ok: true, code: 801, msg: '等待扫码'});
    pollMode = 'normal';
    console.log('PASS 请求超时会提示并准备重试');

    replies = Array.from({length: 3}, () => ({ok: false, error: '已确认扫码，但网易云未返回登录凭证'}));
    await start();
    await poll('正在重试', 1000);
    await page.clock.runFor(2000);
    await page.clock.runFor(2000);
    await expectMessage('请点击“扫码登录”重试');
    const beforeStop = pollCount;
    await page.clock.runFor(6000);
    assert.equal(pollCount, beforeStop);
    console.log('PASS 登录凭证缺失会提示，连续失败后停止重试');

    await start();
    await page.clock.fastForward(180001);
    await expectMessage('二维码已过期');
    console.log('PASS 二维码到期停止轮询');

    await start();
    await page.locator('details:has(#neCookie)').evaluate(element => { element.open = true; });
    await page.locator('#neCookie').fill('MUSIC_U=fixture-token');
    await page.locator('#neCookieBtn').click();
    await page.waitForFunction(() => document.querySelector('#neState').textContent.includes('扫码测试用户'));
    assert.equal(await page.locator('#neCookie').inputValue(), '');
    const beforeCookie = pollCount;
    await page.clock.runFor(6000);
    assert.equal(pollCount, beforeCookie);
    console.log('PASS 手动 Cookie 登录取消扫码并立即显示账号');
    await page.screenshot({path: path.join(root, 'logs/songs-login-ui.png'), fullPage: true});
    await page.goto('http://127.0.0.1:18787/songs');
    await page.waitForFunction(() => !!document.querySelector('.empty'));
    assert.deepEqual(errors, [], '页面不能有未捕获 JavaScript 异常');
    console.log('PASS 点歌列表页面正常，所有交互无未捕获异常');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
