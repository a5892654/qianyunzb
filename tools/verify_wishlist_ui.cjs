// 无浏览器 / 无网络的界面逻辑检查；真实浏览器回归另外执行。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const fullScript = html.split('<script>')[1].split('</script>')[0];
new vm.Script(fullScript);
const code = fullScript.split('// ---------- 心愿单达成动作 ----------')[1].split('// ---------- 点歌 ----------')[0];
const elements = new Map();
function $(selector) {
  if (!elements.has(selector)) elements.set(selector, {textContent: '', style: {}, disabled: false, classList: {contains: () => true}});
  return elements.get(selector);
}
const fields = ['audio', 'song', 'say', 'ai'].map(action => ({dataset: {wishAction: action}, hidden: false}));
const requests = [], notices = [];
let mode = 'success', flushError = false;
const context = vm.createContext({$, $$: () => fields, CFG: {wishlist: {action: 'audio'}},
  setInterval: () => {},
  toast: text => notices.push(text),
  flushConfig: async () => { requests.push('save'); if (flushError) throw Error('设置保存失败'); },
  api: async (url, body) => {
    requests.push(url);
    if (url.endsWith('/test')) return mode === 'success' ? {ok: true, message: '音频已加入播报队列'} : {ok: false, error: '请选择音频文件'};
    return {enabled: true, action: 'audio', busy: false, last_result: {ok: true, time: '12:00:00', message: '音频已加入播报队列'}};
  },
});
vm.runInContext(code, context);

(async () => {
  for (const action of ['audio', 'song', 'say', 'ai']) {
    context.CFG.wishlist.action = action;
    context.renderWishFields();
    assert.deepEqual(fields.filter(field => !field.hidden).map(field => field.dataset.wishAction), [action]);
  }
  console.log('PASS 四种动作只显示各自设置项');
  context.renderWishlist({enabled: false, busy: false});
  assert.ok($('#wishStatus').textContent.includes('尚未启用'));
  context.renderWishlist({enabled: true, busy: false});
  assert.ok($('#wishStatus').textContent.includes('等待'));
  console.log('PASS 未启用和等待消息状态清晰');
  context.renderWishlist({enabled: true, busy: true, latest: {status: 1, wish_name: '今日心愿', items: [{name: '小花花', current: 2, target: 3}]}});
  assert.equal($('#wishTest').disabled, true);
  assert.ok($('#wishItems').textContent.includes('2 / 3'));
  context.renderWishlist({enabled: true, busy: false, latest: {status: 2, wish_name: '今日心愿', items: [{name: '小花花', current: 3, target: 3}]}});
  assert.ok($('#wishStatus').textContent.includes('已达成'));
  assert.ok($('#wishItems').textContent.includes('已达成'));
  console.log('PASS 显示真实进度、达成状态及忙碌状态');
  await $('#wishTest').onclick();
  assert.deepEqual(requests.slice(0, 3), ['save', '/api/wishlist/test', '/api/wishlist/state']);
  assert.equal($('#wishTest').disabled, false);
  assert.ok(notices.at(-1).includes('加入播报队列'));
  console.log('PASS 先保存设置再测试动作，成功后刷新结果');
  mode = 'failure';
  await $('#wishTest').onclick();
  assert.equal($('#wishResult').textContent, '请选择音频文件');
  assert.equal($('#wishResult').style.color, 'var(--red)');
  assert.equal($('#wishTest').disabled, false);
  console.log('PASS 动作失败展示具体错误并恢复测试按钮');
  requests.length = 0; flushError = true;
  await $('#wishTest').onclick();
  assert.deepEqual(requests, ['save']);
  assert.equal($('#wishResult').textContent, '设置保存失败');
  console.log('PASS 保存失败时不执行旧设置中的动作');
  assert.ok(html.includes('data-k="wishlist.audio_file"'));
  assert.ok(html.includes('data-pick="wishlist.audio_file" data-kind="audio"'));
  assert.ok(fullScript.includes("case 'wishlist': renderWishlist(m)"));
  console.log('PASS 音频选择和实时状态消息已接线；完整页面脚本语法正常');
})().catch(error => { console.error(error); process.exitCode = 1; });
