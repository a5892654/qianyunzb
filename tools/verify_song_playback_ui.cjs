// 点歌选择与歌单浏览回归：所有网络请求均由本地替身处理，无账号和音频副作用。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const cfg = JSON.parse(execFileSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', 'import config,json; print(json.dumps(config.DEFAULTS))'], {cwd: root, encoding: 'utf8'}));
const account = {uid: 42, nickname: '歌单测试账号', vip: 0};
const songs = [
  {id: 1, name: '第一首', artist: '歌手 A', dur: 180, playable: true},
  {id: 2, name: '需要额外权限的歌', artist: '歌手 B', dur: 210, playable: false},
  {id: 3, name: '指定的第三首 <原版>', artist: '歌手 C', dur: 200, playable: true},
];
const state = {current: null, paused: false, queue: [], credits: [], account, error: ''};
let socket, qid = 0, addError = '', delayedAdd = false, releaseAdd, delayedPlaylist = false, releasePlaylist;
const errors = [], addBodies = [], controls = [], playlistBodies = [], playlistPlayBodies = [];
const push = () => socket?.send(JSON.stringify({kind: 'songs', ...state}));

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.routeWebSocket('**/ws', ws => { socket = ws; ws.onMessage(() => {}); });
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://127.0.0.1:18787') return route.fulfill({body: ''});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/index.html'), 'utf8')});
      if (url.pathname === '/songs') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/songs.html'), 'utf8')});
      const body = request.method() === 'POST' ? request.postDataJSON() : undefined;
      let result = {};
      if (body && url.pathname.startsWith('/api/songs/')) assert.equal(request.headers()['x-bvb'], '1');
      if (url.pathname === '/api/config' && body) {
        assert.equal(request.headers()['x-bvb'], '1');
        if (body.songs) Object.assign(cfg.songs, body.songs);
        result = {ok: true};
      } else if (url.pathname === '/api/config') result = {...cfg, songs: {...cfg.songs, _ne_logged_in: !!state.account}};
      else if (url.pathname === '/api/l2d/info') result = {cfg: cfg.live2d, expressions: [], motions: [], model_files: []};
      else if (url.pathname === '/api/tts/weights') result = {gpt: [], sovits: []};
      else if (url.pathname === '/api/audio/devices') result = [];
      else if (url.pathname === '/api/songs/state') result = state;
      else if (url.pathname === '/api/broadcast/account') result = {ok: true, logged_in: false};
      else if (url.pathname === '/api/broadcast/sources') result = {ok: true, windows: [], audio: []};
      else if (url.pathname === '/api/broadcast/areas') result = {ok: true, areas: []};
      else if (url.pathname === '/api/broadcast/status') result = {running: false, previewing: false, phase: 'idle'};
      else if (url.pathname === '/api/broadcast/schedule') result = {enabled: false};
      else if (url.pathname === '/api/songs/playlists') result = {ok: true, playlists: [{id: 456, name: '我喜欢的音乐', count: 101}, {id: 789, name: '第二个歌单', count: 3}], more: false, offset: 2};
      else if (url.pathname === '/api/songs/playlist') {
        playlistBodies.push(body);
        const id = Number(body.id), offset = body.offset || 0;
        result = {ok: true, id, name: id === 789 ? '第二个歌单' : '我喜欢的音乐',
          songs: offset ? [{...songs[0], id: 101, name: '第101首'}] : songs, total: id === 789 ? 3 : 101, offset, more: id !== 789 && !offset};
        if (delayedPlaylist && id === 456) {
          delayedPlaylist = false;
          await new Promise(resolve => { releasePlaylist = resolve; });
        }
      } else if (url.pathname === '/api/songs/search') {
        if (body.q === 'bad') return route.fulfill({status: 500, contentType: 'text/html', body: 'upstream failed'});
        result = {ok: true, songs: [songs[2]]};
      } else if (url.pathname === '/api/songs/playlist/play') {
        playlistPlayBodies.push(body);
        state.playlist = {id: Number(body.id), name: Number(body.id) === 789 ? '第二个歌单' : '我喜欢的音乐', total: Number(body.id) === 789 ? 3 : 101, loop: true};
        state.queue = state.queue.filter(s => s.source !== 'autoplay');
        state.queue.push(...songs.filter(s => s.playable).map(s => ({...s, qid: ++qid, source: 'autoplay', by: '歌单自动播放'})));
        result = {ok: true, playlist: state.playlist}; push();
      } else if (url.pathname === '/api/songs/add') {
        addBodies.push(body);
        if (delayedAdd) { delayedAdd = false; await new Promise(resolve => { releaseAdd = resolve; }); }
        if (addError) result = {ok: false, error: addError};
        else {
          const selected = body.ids.map(id => songs.find(s => s.id === Number(id))).filter(s => s?.playable).map(s => ({...s, qid: ++qid, by: '主播', source: body.source || 'request'}));
          if (body.source === 'autoplay') state.queue.push(...selected);
          else if (body.mode === 'play') { state.current = {...selected.shift(), pos: 0, total: 180}; state.queue.unshift(...selected); state.paused = false; }
          else { const index = state.queue.findIndex(s => s.source === 'autoplay'); state.queue.splice(index < 0 ? state.queue.length : index, 0, ...selected); }
          result = {ok: true, added: body.ids.filter(id => Number(id) !== 2).length, skipped: body.ids.filter(id => Number(id) === 2).length, paused: state.paused};
          push();
        }
      } else if (url.pathname === '/api/songs/control') {
        controls.push(body);
        if (body.action === 'play') {
          const index = state.queue.findIndex(s => s.qid === body.qid);
          state.current = {...state.queue.splice(index, 1)[0], pos: 0, total: 180}; state.paused = false;
        } else if (body.action === 'resume') state.paused = false;
        else if (body.action === 'clear') { state.queue = []; state.playlist = null; }
        result = {ok: true}; push();
      } else if (url.pathname === '/api/songs/cookie') { state.account = null; result = {ok: true, account: null}; push(); }
      await route.fulfill({contentType: 'application/json', body: JSON.stringify(result)});
    });
    await page.goto('http://127.0.0.1:18787/');
    await page.locator('nav [data-tab="songs"]').click();
    const followSwitch = page.locator('[data-k="songs.require_follow"]');
    const giftSwitch = page.locator('[data-k="songs.require_gift"]');
    assert.equal(await followSwitch.isChecked(), false);
    let savedRules = page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'POST');
    await followSwitch.check();
    await giftSwitch.check();
    await savedRules;
    assert.equal(cfg.songs.require_follow, true);
    assert.equal(cfg.songs.require_gift, true);
    await page.reload();
    await page.locator('nav [data-tab="songs"]').click();
    assert.equal(await followSwitch.isChecked(), true);
    assert.equal(await giftSwitch.isChecked(), true);
    const rules = page.locator('.card').filter({has: page.getByRole('heading', {name: '点歌规则', exact: true})});
    assert.ok((await rules.textContent()).includes('无需送礼或消耗点歌券'));
    await rules.screenshot({path: path.join(root, 'logs/songs-follow-rules-ui.png')});
    savedRules = page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'POST');
    await followSwitch.uncheck();
    await giftSwitch.uncheck();
    await savedRules;
    assert.equal(cfg.songs.require_follow, false);
    assert.equal(cfg.songs.require_gift, false);
    console.log('PASS 关注点歌开关自动保存、刷新恢复，关闭保留礼物规则设置');
    await page.waitForFunction(() => document.querySelector('#sgPlaylistSelect option[value="456"]'));
    await page.locator('#sgPlaylistSelect').selectOption('456');
    await page.waitForFunction(() => document.querySelector('#sgPlaylistSongs').textContent.includes('指定的第三首'));
    assert.equal(await page.locator('#sgPlaylistSongs [data-song-id="2"]').count(), 0);
    assert.equal(await page.locator('#sgPlaylistSongs script').count(), 0);
    console.log('PASS 登录自动读取歌单，选择后显示歌曲和权限，名称安全转义');

    await page.locator('#sgPlaylistSongs [data-song-id="3"][data-mode="play"]').click();
    await page.waitForFunction(() => document.querySelector('#sgNow').textContent.includes('指定的第三首'));
    assert.deepEqual(addBodies.at(-1), {ids: ['3'], mode: 'play'});
    await page.locator('#sgPlaylistSongs [data-song-id="1"][data-mode="queue"]').click();
    await page.waitForFunction(() => document.querySelector('#sgQueue').textContent.includes('第一首'));
    await page.locator('#sgQueue [data-sgq="play"]').click();
    await page.waitForFunction(() => document.querySelector('#sgNow').textContent.includes('第一首'));
    assert.equal(controls.at(-1).action, 'play');
    console.log('PASS 歌单内指定歌曲立即播放、加入队列、从队列选歌立即播放');

    await page.locator('#sgPlayPlaylist').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('已开启循环播放'));
    assert.deepEqual(playlistPlayBodies.at(-1), {id: 456});
    assert.equal(state.playlist.loop, true);
    assert.equal(state.playlist.total, 101);
    assert.ok((await page.locator('#sgLoopHint').textContent()).includes('正在循环歌单：我喜欢的音乐'));
    assert.equal(await page.locator('#sgLoopHint').isVisible(), true);
    assert.ok((await page.locator('#sgNow').textContent()).includes('第一首'));
    assert.ok((await page.locator('#sgQueue').textContent()).includes('歌单自动播放'));
    assert.equal(await page.locator('#sgQueue [data-sgq="pin"]').count(), 0);
    assert.ok((await page.locator('#toast').textContent()).includes('静默播放，观众点歌优先'));
    assert.equal(await page.locator('#sgPlayPlaylist').textContent(), '播放全部');
    console.log('PASS 播放全部无需额外设置，默认循环整份歌单并显示循环状态');
    await page.locator('#sgPlaylistNext').click();
    await page.waitForFunction(() => document.querySelector('#sgPlaylistSongs').textContent.includes('第101首'));
    assert.equal(playlistBodies.at(-1).offset, 100);
    assert.equal(await page.locator('#sgPlaylistNext').isDisabled(), true);
    await page.locator('#sgPlayPlaylist').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('已开启循环播放'));
    assert.deepEqual(playlistPlayBodies.at(-1), {id: 456});
    await page.locator('#sgPlaylistPrev').click();
    await page.waitForFunction(() => document.querySelector('#sgPlaylistSongs').textContent.includes('指定的第三首'));
    await page.locator('#sgAddPlaylist').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('跳过 1 首'));
    assert.deepEqual(addBodies.at(-1), {ids: ['1', '2', '3'], mode: 'queue', source: 'autoplay'});
    console.log('PASS 列表翻页不改变播放全部的范围，仍可将本页单独加入背景队列');

    await page.locator('#sgQ').fill('https://music.163.com/song?id=3');
    await page.locator('#sgQ').press('Enter');
    await page.waitForFunction(() => document.querySelector('#sgResults').textContent.includes('指定的第三首'));
    addError = '请先开启“启用点歌”';
    await page.locator('#sgResults [data-mode="play"]').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('请先开启'));
    assert.equal(await page.locator('#sgResults [data-mode="play"]').isDisabled(), false);
    addError = '';
    delayedAdd = true;
    const before = addBodies.length;
    await page.locator('#sgResults [data-mode="queue"]').click();
    await page.waitForFunction(() => document.querySelector('#sgResults [data-mode="queue"]').disabled);
    assert.equal(addBodies.length, before + 1);
    releaseAdd();
    await page.waitForFunction(() => !document.querySelector('#sgResults [data-mode="queue"]').disabled);
    assert.equal(state.queue[0].source, 'request');
    await page.locator('#sgQ').fill('bad');
    await page.locator('#sgSearch').click();
    await page.waitForFunction(() => document.querySelector('#sgResults').textContent.includes('HTTP 500'));
    console.log('PASS 歌曲链接检索、错误可见、失败不报成功、请求中禁用重复加入');

    state.current = null; state.paused = true; push();
    await page.waitForFunction(() => document.querySelector('#sgPause').textContent === '继续');
    await page.locator('#sgPause').click();
    await page.waitForFunction(() => document.querySelector('#sgPause').textContent === '暂停');
    state.error = '播放《第一首》失败：不能完整播放'; push();
    await page.waitForFunction(() => !document.querySelector('#sgError').hidden);
    assert.ok((await page.locator('#sgError').textContent()).includes('不能完整播放'));
    await page.locator('[data-sg="clear"]').click();
    await page.waitForFunction(() => document.querySelector('#sgLoopHint').hidden);
    assert.equal(state.playlist, null);
    console.log('PASS 空闲暂停可继续，真实播放错误显示在播放区');

    delayedPlaylist = true;
    await page.locator('#sgPlaylistInput').fill('456');
    await page.locator('#sgLoadPlaylist').click();
    await page.waitForFunction(() => document.querySelector('#sgPlaylistSongs').textContent.includes('正在读取'));
    await page.locator('#sgPlaylistSelect').selectOption('789');
    await page.waitForFunction(() => document.querySelector('#sgPlaylistTitle').textContent.includes('第二个歌单'));
    releasePlaylist();
    await page.waitForTimeout(50);
    assert.ok((await page.locator('#sgPlaylistTitle').textContent()).includes('第二个歌单'));
    console.log('PASS 切换歌单后旧响应不能覆盖新选择');
    state.error = ''; push();
    await page.screenshot({path: path.join(root, 'logs/songs-playback-ui.png'), fullPage: true});
    await page.setViewportSize({width: 900, height: 1100});
    await page.screenshot({path: path.join(root, 'logs/songs-playback-narrow.png'), fullPage: true});
    await page.locator('#neLogout').click();
    await page.waitForFunction(() => document.querySelector('#sgPlaylistTools').hidden);
    assert.equal(await page.locator('#sgPlaylistSelect option').count(), 1);
    state.current = {...songs[0], by: '歌单自动播放', source: 'autoplay', pos: 0, total: 180};
    await page.goto('http://127.0.0.1:18787/songs');
    await page.waitForFunction(() => document.querySelector('.sub')?.textContent.includes('歌单自动播放'));
    assert.ok(!(await page.locator('.sub').textContent()).includes('点歌：'));
    assert.deepEqual(errors, []);
    console.log('PASS 退出登录清除旧账号歌单，OBS 歌单区分自动播放，页面无未捕获异常');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
