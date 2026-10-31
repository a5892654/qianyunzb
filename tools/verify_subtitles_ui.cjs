// 离线验证底部字幕开关；使用本地页面和模拟事件，不连接直播平台。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 200}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    let socket, enabled = true;
    await page.routeWebSocket('**/ws', ws => { socket = ws; });
    await page.route('http://127.0.0.1:18787/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/overlay') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/overlay.html'), 'utf8')});
      if (url.pathname === '/api/config') return route.fulfill({json: {overlay: {enabled, seconds: 0.1}}});
      return route.fulfill({status: 404});
    });
    const send = data => socket.send(JSON.stringify(data));
    const visible = () => page.locator('#box').evaluate(el => el.classList.contains('on'));
    await page.goto('http://127.0.0.1:18787/overlay');
    await page.waitForFunction(() => enabled === true);
    send({kind: 'speak', text: '字幕开关测试'});
    await page.waitForFunction(() => document.querySelector('#box').classList.contains('on'));
    enabled = false;
    send({kind: 'overlay_config', enabled, seconds: 0.1});
    await page.waitForFunction(() => !document.querySelector('#box').classList.contains('on'));
    assert.equal(await page.locator('#box').textContent(), '');
    send({kind: 'speak', text: '关闭时不应显示'});
    send({kind: 'speak_end'});
    assert.equal(await visible(), false);
    await page.reload();
    await page.waitForFunction(() => settingsVersion > 0);
    assert.equal(await page.evaluate(() => enabled), false);
    send({kind: 'speak', text: '刷新后仍然关闭'});
    assert.equal(await visible(), false);
    enabled = true;
    send({kind: 'overlay_config', enabled, seconds: 0.1});
    await page.waitForFunction(() => enabled === true);
    assert.equal(await visible(), false, '重新开启不应重放旧字幕');
    send({kind: 'speak', text: '重新开启字幕'});
    await page.waitForFunction(() => document.querySelector('#box').classList.contains('on'));
    send({kind: 'speak_end'});
    await page.waitForFunction(() => !document.querySelector('#box').classList.contains('on'));
    send({kind: 'speak', text: '退出时隐藏'});
    await page.waitForFunction(() => document.querySelector('#box').classList.contains('on'));
    send({kind: 'bye'});
    await page.waitForFunction(() => document.documentElement.classList.contains('off'));
    assert.deepEqual(errors, []);
    console.log('PASS subtitle disable, ignored speech, persisted reload, re-enable, hold time and shutdown');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
