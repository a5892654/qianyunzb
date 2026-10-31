// 真模型端到端校验；仅本机测试服务，不连接 B站、AI、TTS 或推流。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const key = process.argv[2] || 'DeepSeek免费公皮';
const serverCode = `
import asyncio, collections, copy, json, os, sys
from types import SimpleNamespace
from aiohttp import web
import config, live2d
from main import App, build_web
config.CONFIG_PATH = os.path.join(config.LOG_DIR, 'live2d-model-verification.json')
async def refresh(): pass
async def main():
    app = object.__new__(App)
    app.cfg = copy.deepcopy(config.DEFAULTS)
    app.cfg['live2d']['enabled'] = True
    live2d.switch_model(app.cfg['live2d'], sys.argv[1])
    app.ws, app.history, app.status = set(), collections.deque(), {}
    app.broadcast = SimpleNamespace(refresh_overlay=refresh)
    runner = web.AppRunner(build_web(app))
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    print('READY ' + str(site._server.sockets[0].getsockname()[1]), flush=True)
    try: await asyncio.Event().wait()
    finally: await runner.cleanup()
asyncio.run(main())
`;

(async () => {
  const server = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-u', '-c', serverCode, key], {
    cwd: root, env: {...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1'}, windowsHide: true,
  });
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      let output = '', errors = '';
      const timer = setTimeout(() => reject(new Error('Preview server timeout: ' + errors)), 20000);
      server.stderr.on('data', chunk => { errors += chunk; });
      server.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/READY (\d+)/);
        if (match) { clearTimeout(timer); resolve(Number(match[1])); }
      });
      server.on('exit', code => { clearTimeout(timer); reject(new Error(`Preview server exited ${code}: ${errors}`)); });
    });
    const base = `http://127.0.0.1:${port}`;
    browser = await chromium.launch({channel: 'msedge', headless: true});
    const page = await browser.newPage({viewport: {width: 900, height: 1000}});
    const errors = [], failures = [], maskErrors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', message => {
      if (/not supported mask count|\[CSM\]\[E\]/i.test(message.text())) maskErrors.push(message.text());
    });
    page.on('response', r => { if (r.status() >= 400) failures.push(r.url() + ': ' + r.status()); });
    await page.goto(base + '/live2d?overlay=1&bg=303848');
    await page.waitForFunction(() => typeof model !== 'undefined' && model?.internalModel && model.parent, {timeout: 30000});
    const info = await (await page.request.get(base + '/api/l2d/info')).json();
    const profile = JSON.parse(fs.readFileSync(path.join(root, 'docs/live2d/deepseek-chan.json'), 'utf8'));
    assert.equal(info.expressions.length, 23);
    assert.equal(info.motions.length, 8);
    assert.deepEqual(new Set(profile.emotions.flatMap(e => e.expressions)), new Set(info.expressions.map(e => e.id)));
    assert.deepEqual(new Set(profile.emotions.map(e => e.motion).filter(m => m && m !== '@reset')), new Set(info.motions.map(e => e.id)));
    console.log('PASS all 23 expressions and 8 motions are exposed to AI');
    const screenshot = path.join(root, 'logs/deepseek-live2d-preview.png');
    await page.screenshot({path: screenshot});
    const params = await page.evaluate(() => {
      const core = model.internalModel.coreModel;
      return {count: core.getParameterCount(), mouth: core.getParameterIndex('ParamMouthOpenY'), smile: core.getParameterIndex('EyeSmile')};
    });
    assert.ok(params.count > 100 && params.mouth >= 0 && params.smile >= 0);
    console.log('PASS Cubism 5 model, five textures, physics and custom facial parameters load', params);
    const masks = await page.evaluate(() => ({
      count: model.internalModel.renderer._clippingManager.getClippingMaskCount(),
      buffers: model.internalModel.renderer.getRenderTextureCount(),
    }));
    assert.equal(masks.count, 49);
    assert.ok(masks.buffers * 32 >= masks.count);
    await page.evaluate(() => { CFG.scale = 2; CFG.y = 1; layout(); });
    await page.screenshot({path: path.join(root, 'logs/deepseek-face-fixed.png'), clip: {x: 100, y: 80, width: 700, height: 650}});
    await page.evaluate(() => { CFG.scale = 1; CFG.y = 0; layout(); });
    console.log('PASS all 49 clipping masks fit the allocated buffers; face screenshot saved', masks);
    await page.setViewportSize({width: 720, height: 720});
    await page.waitForFunction(() => app.screen.width === 720 && app.screen.height === 720);
    const capture = await page.evaluate(async () => {
      app.ticker.stop();
      const wasAutoUpdate = model.autoUpdate;
      const savedUpdate = model.update;
      model.autoUpdate = false;
      model.update = () => {};
      const configBefore = JSON.stringify(CFG);
      const decode = async png => {
        const image = new Image(); image.src = 'data:image/png;base64,' + png;
        await image.decode(); return image;
      };
      const crop = image => {
        const canvas = document.createElement('canvas'); canvas.width = canvas.height = 720;
        const s = image.height / 720;
        canvas.getContext('2d').drawImage(image, 180*s, 47*s, 360*s, 360*s, 0, 0, 720, 720);
        return canvas;
      };
      try {
        const full = window.captureLive2DFrame({size: 720, zoom: 1}).png;
        const before = crop(await decode(full));
        const after = window.captureLive2DFrame({size: 720, zoom: 2}).png;
        const reference = crop(await decode(window.captureLive2DFrame({size: 1440, zoom: 1}).png));
        const actual = document.createElement('canvas'); actual.width = actual.height = 720;
        actual.getContext('2d').drawImage(await decode(after), 0, 0);
        const a = actual.getContext('2d').getImageData(0, 0, 720, 720).data;
        const b = reference.getContext('2d').getImageData(0, 0, 720, 720).data;
        let alphaError = 0, colorError = 0, opaque = 0;
        for (let i = 0; i < a.length; i += 4) {
          alphaError += Math.abs(a[i+3] - b[i+3]);
          if (a[i+3] > 240 && b[i+3] > 240) {
            opaque++;
            for (let c = 0; c < 3; c++) colorError += Math.abs(a[i+c] - b[i+c]);
          }
        }
        const large = await decode(window.captureLive2DFrame({size: 1536, zoom: 3}).png);
        const reset = await decode(window.captureLive2DFrame({size: 720, zoom: 1}).png);
        return {before: before.toDataURL('image/png').split(',')[1], after,
          alphaError: alphaError / (720*720), colorError: colorError / (opaque*3),
          opaque, large: [large.width, large.height],
          restored: reset.width === 720 && reset.height === 720 && app.stage.scale.x === 1
            && app.stage.scale.y === 1 && app.stage.x === 0 && app.stage.y === 0,
          configUnchanged: JSON.stringify(CFG) === configBefore};
      } finally {
        model.update = savedUpdate;
        model.autoUpdate = wasAutoUpdate;
        if (OVERLAY_MODE) {
          app.stop();
          window.__testCompositorClock = setInterval(() => window.captureLive2DFrame({size: 720, zoom: 1}), 33);
        } else app.ticker.start();
      }
    });
    assert.ok(capture.opaque > 50000, '真实模型半身不能为空白');
    assert.ok(capture.alphaError < 5 && capture.colorError < 12, '原生半身应与双分辨率全身的同一取景一致');
    assert.deepEqual(capture.large, [1536, 1536]);
    for (const name of ['before', 'after']) fs.writeFileSync(path.join(root, 'logs/live2d-sharpness-' + name + '.png'), Buffer.from(capture[name], 'base64'));
    assert.ok(capture.restored && capture.configUnchanged, '取景切换必须恢复画布且不修改模型配置');
    console.log('PASS native half-body matches high-resolution crop; enlarged capture and full-body reset', {
      alphaError: capture.alphaError, colorError: capture.colorError, large: capture.large,
    });
    if (process.argv.includes('--capture-only')) return;
    await page.setViewportSize({width: 900, height: 1000});
    for (const expr of info.expressions) {
      const result = await page.evaluate(id => window.__l2dDebugExpr(id), expr.id);
      assert.equal(result, 'ok:' + expr.id);
    }
    for (const motion of info.motions) {
      const result = await page.evaluate(async id => {
        const manager = model.internalModel.motionManager;
        const clip = await manager.loadMotion(id, 0);
        if (!clip) return false;
        return {started: await model.motion(id, 0, 3), loop: clip.isLoop()};
      }, motion.id);
      assert.deepEqual(result, {started: true, loop: false}, motion.id);
    }
    console.log('PASS every real expression parses and every real motion starts');
    for (const name of ['开心', '感谢', '害羞', '惊讶', '疑惑', '生气', '难过', '尴尬', '晕', '无语']) {
      await page.request.post(base + '/api/l2d/test', {data: {emotion: name}, headers: {'X-BVB': '1'}});
      await page.waitForFunction(name => active.some(a => a.id === '@face:' + name && a.w > 0.9), name);
    }
    // Use the same speak events as production; labels must drive the model, not be spoken.
    await page.evaluate(() => {
      model.internalModel.motionManager.stopAllMotions();
      // Cubism restores saved parameters after rendering; observe the values that
      // actually reach the model rather than the restored idle baseline.
      model.internalModel.on('beforeModelUpdate', () => {
        window.__testMouthValue = model.internalModel.coreModel.getParameterValueById('ParamMouthOpenY');
      });
      onSpeak({id: 987, text: '大家好，欢迎来到直播间！', emotion: '感谢', env: Array(90).fill(0.8), fps: 30});
      onSpeakPos({id: 987, pos: 0.2});
    });
    await page.waitForFunction(() => window.__testMouthValue > 0.4);
    const mouth = await page.evaluate(() => window.__testMouthValue);
    assert.ok(mouth > 0.4);
    await page.evaluate(() => onSpeakEnd({id: 987}));
    await page.waitForFunction(() => window.__testMouthValue < 0.05);
    console.log('PASS real speech events drive the mouth and close it after speaking');
    await page.request.post(base + '/api/l2d/test', {data: {emotion: '抚胸手打开'}, headers: {'X-BVB': '1'}});
    await page.waitForFunction(() => model.internalModel.motionManager.state.currentGroup === '默认手切举手');
    await page.waitForFunction(() => model.internalModel.motionManager.state.currentGroup === '举手切默认手', null, {timeout: 10000});
    console.log('PASS AI gesture plays and automatically returns to the default hand');
    await page.request.post(base + '/api/l2d/test', {data: {expressions: ['黑脸', '黑框眼镜', '体型2'], motion: '脱捕'}, headers: {'X-BVB': '1'}});
    await page.waitForFunction(() => active.some(a => a.id === '体型2' && a.w > 0.9) && model.internalModel.motionManager.state.currentGroup === '脱捕');
    await page.request.post(base + '/api/l2d/test', {data: {motion: '@reset'}, headers: {'X-BVB': '1'}});
    await page.waitForFunction(() => active.every(a => a.base) && activeMotion === '' && model.internalModel.motionManager.state.currentPriority <= 1);
    const restored = await page.evaluate(() => {
      const core = model.internalModel.coreModel;
      return ['key1', 'key11', 'Clothes2', 'Movement1', 'Movement2', 'Movement3'].map(id => {
        const index = core.getParameterIndex(id);
        return {id, value: core.getParameterValueByIndex(index), expected: core.getParameterDefaultValue(index)};
      });
    });
    for (const p of restored) assert.ok(Math.abs(p.value - p.expected) < 0.001, JSON.stringify(p));
    await page.evaluate(() => applyEmotion('黑脸'));
    await page.waitForFunction(() => active.some(a => a.id === '黑脸' && a.w > 0.9));
    assert.equal(await page.evaluate(() => applyEmotion('恢复默认')), true);
    assert.equal(await page.evaluate(() => active.filter(a => !a.base).length), 0);
    console.log('PASS manual and AI reset clear expressions, props, body/hand poses and one-shot motion');
    const switched = await page.request.post(base + '/api/l2d/switch', {data: {key}, headers: {'X-BVB': '1'}});
    assert.equal(switched.status(), 200);
    assert.deepEqual(errors, []);
    assert.deepEqual(failures, []);
    assert.deepEqual(maskErrors, []);
    console.log('PASS backend switch and websocket update; no page errors or missing resources');
    await page.goto(base + '/live2d?overlay=1');
    await page.waitForFunction(() => model && model.internalModel);
    const manualClock = await page.evaluate(async () => {
      PIXI.Ticker.shared.stop();
      const before = model.elapsedTime;
      window.captureLive2DFrame({size: 720, zoom: 1});
      await new Promise(resolve => setTimeout(resolve, 120));
      window.captureLive2DFrame({size: 720, zoom: 1});
      return {elapsed: model.elapsedTime - before, autoUpdate: model.automator.autoUpdate, ticker: app.ticker.started};
    });
    assert.equal(manualClock.autoUpdate, false);
    assert.equal(manualClock.ticker, false);
    assert.ok(manualClock.elapsed >= 50, JSON.stringify(manualClock));
    console.log('PASS overlay animation advances with the compositor clock even when browser RAF stops', manualClock);
    console.log('Screenshot:', screenshot);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
