// Render the installed public skin against the wage scene, without starting the application.
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright-core');
const root = path.resolve(__dirname, '..');
const out = path.join(root, 'outputs/wage-preview');
fs.mkdirSync(out, {recursive: true});
const data = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', `import config,live2d,json,copy; from pathlib import Path
p=Path('config.json'); raw=json.loads(p.read_text(encoding='utf8')) if p.is_file() else {}; c=config.merge(config.DEFAULTS['live2d'],raw.get('live2d',{}))
if not c.get('model_file'):
 live2d.switch_model(c,next(x['key'] for x in live2d.list_library() if x['key'].startswith('DeepSeek')))
e,m=live2d.list_assets(c['model_dir']); print(json.dumps({'info':{'cfg':c,'expressions':e,'motions':m,'model_files':[]},'model':live2d.build_model_json(c)}))`], {cwd: root, encoding: 'utf8'}));
data.info.cfg.enabled = true;
(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1080, height: 1080}});
    await page.routeWebSocket('**/ws', ws => ws.onMessage(() => {}));
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.origin !== 'http://127.0.0.1:18788') return route.abort();
      if (u.pathname === '/live2d') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/live2d.html'), 'utf8')});
      if (u.pathname === '/api/l2d/info') return route.fulfill({json: data.info});
      if (u.pathname === '/l2d/model/auto.model3.json') return route.fulfill({json: data.model});
      let file;
      if (u.pathname.startsWith('/static/vendor/')) file = path.join(root, 'web/vendor', path.basename(u.pathname));
      else if (u.pathname.startsWith('/l2d/model/')) {
        const rel = decodeURIComponent(u.pathname.slice('/l2d/model/'.length));
        file = path.resolve(data.info.cfg.model_dir, rel);
        if (!file.startsWith(path.resolve(data.info.cfg.model_dir) + path.sep)) return route.abort();
      } else return route.abort();
      return route.fulfill({path: file});
    });
    await page.goto('http://127.0.0.1:18788/live2d?overlay=1');
    const view = {size: 1024, zoom: 1.8, office: {status: 'working', paused: false}};
    await page.waitForFunction(view => window.captureLive2DFrame?.(view).png, view, {timeout: 40000});
    for (const kind of ['working', 'slacking', 'caught', 'poll']) {
      view.office.status = kind === 'poll' ? 'working' : kind;
      let result;
      for (let i = 0; i < 14; i++) {
        result = await page.evaluate(view => window.captureLive2DFrame(view), view);
        await page.waitForTimeout(70);
      }
      if (!result.png) throw new Error(JSON.stringify(result));
      fs.writeFileSync(path.join(out, `avatar-${kind}.png`), Buffer.from(result.png, 'base64'));
      if (kind === 'working') fs.writeFileSync(path.join(out, 'avatar.png'), Buffer.from(result.png, 'base64'));
    }
    execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', `import cv2,time; from types import SimpleNamespace; from live_overlay import OverlayCompositor; from live_wage import WageRenderer,demo_state; from wage import avatar_layout,DEFAULTS; from pathlib import Path; p=Path('outputs/wage-preview'); avatar=None; r=WageRenderer();s=demo_state();c=OverlayCompositor(SimpleNamespace(frame=lambda: avatar),lambda:avatar_layout(DEFAULTS,s));
for kind in ('working','caught','poll'):
 avatar=cv2.imread(str(p/('avatar-'+kind+'.png')),-1); s.update(status='caught' if kind=='caught' else 'working',wage=-500 if kind=='caught' else 4900,deduction=500,poll={'left':21,'yes':8,'no':3,'result':''} if kind=='poll' else None); f=r.render(s,foreground=False); c.apply(f); r.apply_overlays(f,s,time.monotonic()); cv2.imwrite(str(p/(kind+'-with-model.png')),f)
print('PASS: public Live2D skin rendered and composed with working/caught/poll scenes')`], {cwd: root, stdio: 'inherit'});
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
