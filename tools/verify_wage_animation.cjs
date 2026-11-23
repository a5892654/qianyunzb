// Use the actual installed skin and production WebGL/message handlers; no live account or audio output.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright-core');
const root = path.resolve(__dirname, '..');
const out = path.join(root, 'outputs/wage-animation');
fs.mkdirSync(out, {recursive: true});
const fixture = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-X', 'utf8', '-c', `
import config,live2d,json
from pathlib import Path
from wage_slack import SLACK_BY_ID
raw=json.loads(Path('config.json').read_text(encoding='utf8'))
c=config.merge(config.DEFAULTS['live2d'],raw['live2d'])
e,m=live2d.list_assets(c['model_dir'])
print(json.dumps({'info':{'cfg':c,'expressions':e,'motions':m},'model':live2d.build_model_json(c),'slack_kinds':list(SLACK_BY_ID)}))
`], {cwd: root, encoding: 'utf8'}));
Object.assign(fixture.info.cfg, {enabled: true, sway: false, expr_hold: 0.15});

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const errors = [];
  let messages;
  try {
    const page = await browser.newPage({viewport: {width: 720, height: 720}});
    page.on('pageerror', e => errors.push(e.message));
    await page.routeWebSocket('**/ws', ws => { messages = ws; ws.onMessage(() => {}); });
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.origin !== 'http://127.0.0.1:18788') return route.abort();
      if (u.pathname === '/live2d') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/live2d.html'), 'utf8')});
      if (u.pathname === '/api/l2d/info') return route.fulfill({json: fixture.info});
      if (u.pathname === '/l2d/model/auto.model3.json') return route.fulfill({json: fixture.model});
      let file;
      if (u.pathname.startsWith('/static/vendor/')) file = path.join(root, 'web/vendor', path.basename(u.pathname));
      else if (u.pathname.startsWith('/l2d/model/')) {
        file = path.resolve(fixture.info.cfg.model_dir, decodeURIComponent(u.pathname.slice('/l2d/model/'.length)));
        if (!file.startsWith(path.resolve(fixture.info.cfg.model_dir) + path.sep)) return route.abort();
      } else return route.abort();
      return route.fulfill({path: file});
    });
    await page.goto('http://127.0.0.1:18788/live2d?overlay=1');
    let view = {size: 720, zoom: 1.8, office: {status: 'working', paused: false, work_time: 0}};
    await page.waitForFunction(v => !!window.captureLive2DFrame?.(v).png, view, {timeout: 45000});
    await page.waitForFunction(() => typeof onSpeak === 'function');
    // Cubism restores saved motion parameters after drawing. Observe the actual
    // values submitted to the renderer, rather than that restored baseline.
    await page.evaluate(() => model.internalModel.on('beforeModelUpdate', () => {
      const core = model.internalModel.coreModel;
      window.__wageDrawnParams = Object.fromEntries(['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'AngleX', 'AngleY', 'ParamEyeLOpen', 'ParamEyeROpen', 'ParamMouthOpenY', 'Param60', 'Param41'].map(k => [k, core.getParameterValueById(k)]));
    }));
    const frames = [];
    const samples = [];
    async function sample(label, count) {
      for (let i = 0; i < count; i++) {
        if (view.office.status === 'working' && !view.office.paused)
          view.office.work_time = (Number(view.office.work_time) || 0) + 1 / 12;
        const result = await page.evaluate(v => {
          const frame = window.captureLive2DFrame(v);
          const core = model.internalModel.coreModel;
          const palm = core.getModel().drawables.ids.indexOf('ArtMesh302');
          return {...frame, params: window.__wageDrawnParams, gesture: activeMotion, time: officeTime,
            typing: typingWeight, workTime: v.office.work_time, handMirror: !!typingHandLayer?.visible,
            originalPalm: palm < 0 ? null : core.getDrawableOpacity(palm)};
        }, view);
        assert.ok(result.png, JSON.stringify(result));
        samples.push({label, ...result.params, gesture: result.gesture, time: result.time, typing: result.typing,
          workTime: result.workTime, handMirror: result.handMirror, originalPalm: result.originalPalm});
        const name = `${String(frames.length).padStart(3, '0')}-${label}.png`;
        fs.writeFileSync(path.join(out, name), Buffer.from(result.png, 'base64'));
        frames.push(name);
        await page.waitForTimeout(70);
      }
    }
    const send = data => messages.send(JSON.stringify(data));
    await sample('working', 55);
    const work = samples.filter(s => s.label === 'working');
    const range = (rows, k) => Math.max(...rows.map(s => s[k])) - Math.min(...rows.map(s => s[k]));
    const angleX = fixture.info.cfg.parameter_map?.angle_x || 'ParamAngleX';
    console.log('Work angle ranges', Object.fromEntries(['ParamAngleX', 'AngleX', 'ParamAngleY', 'AngleY'].map(k => [k, range(work, k)])));
    assert.ok(range(work, angleX) > 0.3, 'Work animation must move even with global sway disabled');
    assert.ok(work.at(-1).typing > .95, 'Typing pose must engage while working');
    assert.ok(work.at(-1).handMirror && work.at(-1).originalPalm < .001,
      'Typing must show the hand-back meshes and suppress the palm-facing pointing hand');
    const active = work.filter(s => s.typing > .95 && s.workTime % 4.8 < 4.35);
    assert.ok(range(active, 'Param60') > .01 && range(active, 'Param41') > .01,
      'Both hands must actually alternate keystrokes');
    const rest = work.filter(s => s.workTime % 4.8 >= 4.45 && s.workTime % 4.8 <= 4.7);
    assert.ok(rest.length >= 2, 'Observe a natural pause during sustained typing');
    assert.ok(range(rest, 'Param60') < .001 && range(rest, 'Param41') < .001,
      'Both hands should briefly rest between typing bursts');
    view.office.paused = true;
    await sample('paused-working', 4);
    assert.ok(range(samples.slice(-4), 'Param60') < .001 && range(samples.slice(-4), 'Param41') < .001,
      'Pausing must freeze both typing hands');
    view.office.paused = false;
    await sample('resumed-working', 2);
    send({kind: 'speak', id: 'voice-test', text: '鲨总，我真的没有摸鱼！', emotion: '惊讶', fps: 30, env: [], stream: true});
    await sample('awaiting-audio', 3);
    assert.ok(samples.slice(-3).every(s => s.ParamMouthOpenY < 0.03), 'No mouth movement before streamed audio arrives');
    send({kind: 'speak_env', id: 'voice-test', env: Array.from({length: 150}, (_, i) => (Math.sin(i * 0.8) + 1) * 0.4)});
    send({kind: 'speak_pos', id: 'voice-test', pos: 0});
    await sample('talking', 24);
    const spoken = samples.filter(s => s.label === 'talking');
    assert.ok(spoken.slice(6).every(s => s.typing < .001), 'Typing must smoothly release for speech gestures');
    assert.ok(spoken.slice(6).every(s => !s.handMirror), 'Speaking must return to the original gesture hands');
    assert.ok(range(spoken, 'ParamMouthOpenY') > 0.15, 'Real speak_env / speak_pos handlers must drive the mouth');
    assert.ok(spoken.some(s => s.gesture), 'Speech must select a reversible gesture on this skin');
    send({kind: 'speak_end', id: 'voice-test'});
    await sample('voice-ended', 18);
    assert.ok(samples.at(-1).ParamMouthOpenY < 0.03, 'Mouth must close when speech ends');
    assert.equal(samples.at(-1).gesture, '', 'Gesture must return to default after speech');
    view.office.status = 'slacking';
    await sample('slacking', 15);
    assert.ok(samples.at(-1)[angleX] > 1, 'Slacking must turn the head toward the audience');
    assert.ok(samples.at(-1).typing < .001, 'Slacking must stop typing');
    view.office.status = 'caught';
    await sample('caught', 15);
    assert.ok(samples.at(-1)[angleX] < -1, 'Caught reaction must differ from slacking');
    assert.ok(samples.at(-1).typing < .001, 'Caught reaction must stop typing');
    view.office.paused = true;
    await sample('paused', 4);
    assert.equal(samples.at(-1).time, samples.at(-4).time, 'Pausing must freeze the office animation clock');
    view.office.paused = false;
    view.office.status = 'slacking';
    for (const kind of fixture.slack_kinds) {
      view.office.slack_kind = kind;
      await sample('slack-' + kind, 15);
      assert.ok(samples.at(-1).typing < .001, kind + ' must not type');
      assert.ok(samples.slice(-5).every(s => s.ParamMouthOpenY < .03), kind + ' must not fake speech');
    }
    const poseRows = kind => samples.filter(s => s.label === 'slack-' + kind).slice(-5);
    const mean = (kind, key) => poseRows(kind).reduce((sum, s) => sum + s[key], 0) / 5;
    const angleY = fixture.info.cfg.parameter_map?.angle_y || (angleX === 'AngleX' ? 'AngleY' : 'ParamAngleY');
    assert.ok(mean('daydream', angleY) - mean('nap', angleY) > 12, 'Daydream must look up and nap must look down');
    assert.ok(mean('daydream', angleX) - mean('game', angleX) > 8, 'Game and daydream must have distinct gaze directions');
    assert.ok(poseRows('nap').every(s => s.ParamEyeLOpen < .2 && s.ParamEyeROpen < .2), 'Nap must close both eyes');
    assert.ok(poseRows('daydream').some(s => s.ParamEyeLOpen > .5), 'Eyes must reopen after nap');
    view.office = {status: 'working', paused: false, typing: false};
    await sample('typing-disabled', 8);
    assert.ok(samples.at(-1).typing < .001, 'Typing switch must disable the hand animation');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(out, 'samples.json'), JSON.stringify({frames, samples}, null, 2));
    console.log(`PASS: actual skin typing and speech priority, ${fixture.slack_kinds.length} slack poses, nap eyes close and reopen, pause and mouth/gesture reset.`);
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exit(1);});
