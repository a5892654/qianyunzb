"""上班摸鱼的办公室玩法；所有计时由节目帧推进，不联网、不执行屏幕代码。"""
from __future__ import annotations

import math
import random
import re

OFFICE_DEFAULTS = {
    'rescue_enabled': True, 'rescue_seconds': 10,
    'tasks_enabled': True, 'task_interval_seconds': 180, 'task_seconds': 35,
    'items_enabled': True, 'rules_enabled': True, 'rule_seconds': 30,
    'career_enabled': True,
}
ITEMS = {
    'coffee': ('咖啡', 3), 'shield': ('挡箭牌', 5),
    'keyboard': ('坏键盘', 3), 'pillow': ('枕头', 3),
}
RANKS = ((0, '实习生'), (300, '正式员工'), (900, '组长'), (1800, '经理'), (3000, '临时老板'))
RULES = (
    {'id': 'no_boss', 'title': '不能说“老板”或“鲨总”'},
    {'id': 'short', 'title': '每句最多五个字'},
    {'id': 'no_tsundere', 'title': '不能说“哼”和“才不是”'},
)
TASKS = (
    ('修好 Bug，但不许改代码', ('先补测试', '确认需求', '假装没听见'), (160, 80, -80)),
    ('做一个既显眼又不能被看见的按钮', ('写验收标准', '先做透明按钮', '甩锅给显示器'), (160, 60, -60)),
    ('下班前交一份没有文字的周报', ('交图表', '拉会讲解', '交空白文档'), (160, 80, -60)),
    ('把昨天删掉的需求今天加回来', ('找版本记录', '先问清楚', '当场装死'), (160, 100, -80)),
)


def career_defaults():
    return {'xp': 0, 'wallet': 5, 'shifts': 0, 'tasks': 0, 'rescues': 0,
            'debt_peak': 0, 'debt_clears': 0, 'boss_day': '', 'armed_shield': False,
            'items': {key: 0 for key in ITEMS}}


def clean_career(raw):
    result = career_defaults()
    if not isinstance(raw, dict):
        return result
    for key in result:
        if key in ('items', 'boss_day', 'armed_shield'):
            continue
        value = raw.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            result[key] = max(0, min(1_000_000_000, int(value)))
    if isinstance(raw.get('items'), dict):
        for key in ITEMS:
            value = raw['items'].get(key)
            if type(value) is int:
                result['items'][key] = max(0, min(99, value))
    if isinstance(raw.get('boss_day'), str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw['boss_day']):
        result['boss_day'] = raw['boss_day']
    result['armed_shield'] = raw.get('armed_shield') is True
    return result


def rank_name(career):
    return next(name for need, name in reversed(RANKS) if career.get('xp', 0) >= need)


class OfficePlay:
    def __init__(self, engine):
        self.engine = engine
        self.elapsed = 0.0
        self.round = None
        self.rule = None
        self.result = None
        self.coffee_left = self.keyboard_left = self.pillow_left = 0.0
        self.shield = self.career['armed_shield']
        self.pose = ''
        self.pose_left = 0.0
        self.task_wait = 0.0
        self._last_starts = {}
        self._in_debt = engine.state['wage'] < 0
        self._debt_rewarded = False
        self._deferred_inspection = False

    @property
    def career(self):
        return self.engine.state['career']

    def settings(self):
        return self.engine.settings()

    def _notice(self, title, text='', pose='inspect'):
        self.result = {'title': title, 'text': text, 'left': 8}
        self.pose, self.pose_left = pose, 8

    def _cue(self, kind, **data):
        self.engine._cue(kind, **data)

    def _grant_xp(self, amount):
        if not self.settings()['career_enabled']:
            return
        previous = rank_name(self.career)
        self.career['xp'] += int(amount)
        current = rank_name(self.career)
        if current != previous:
            if current == '临时老板':
                self.career['boss_day'] = self.engine._today()
            self._notice('升职了！', f'{previous} → {current}', 'approve')
            self._cue('promotion', rank=current)
        self.engine._dirty = True

    def on_balance(self):
        wage = self.engine.state['wage']
        if not self.settings()['career_enabled']:
            self._in_debt = wage < 0
            return
        if wage < 0:
            self.career['debt_peak'] = max(self.career['debt_peak'], int(math.ceil(-wage)))
            if not self._in_debt:
                self._cue('debt_start', debt=int(math.ceil(-wage)))
            self._in_debt = True
        elif self._in_debt:
            self._in_debt = False
            self.career['debt_clears'] += 1
            self._notice('债还清了！', '从倒欠公司重新回到正工资', 'approve')
            self._cue('debt_cleared')
            if not self._debt_rewarded:
                self._debt_rewarded = True
                self._grant_xp(100)

    def _money(self, amount):
        if self.engine.state['status'] == 'offwork':
            return
        self.engine.state['wage'] += amount
        if amount:
            self.engine._floater(f'{amount:+g}')
        self.on_balance()
        if self.engine.state['wage'] >= self.settings()['target']:
            self.engine._go_offwork(self.settings())

    def _busy(self):
        return self.round is not None or self.engine._poll is not None

    def _start(self, kind, title, choices, seconds, **extra):
        if self._busy() or self.engine.state['status'] == 'offwork':
            return False
        if kind != 'rescue' and self.elapsed - self._last_starts.get(kind, -1e9) < 30:
            return False
        self._last_starts[kind] = self.elapsed
        self.round = {'kind': kind, 'title': title, 'choices': list(choices),
                      'left': float(seconds), 'votes': {}, **extra}
        self.result = None
        self._cue(kind + '_start', title=title)
        return True

    def begin_inspection(self):
        if not self.settings()['rescue_enabled']:
            return False
        if self.round and self.round['kind'] == 'rescue':
            return True
        # 查岗不能挤掉正在进行的投票。当前事件延后到本轮互动结束。
        if self._busy():
            self._deferred_inspection = True
            return True
        seconds = self.settings()['rescue_seconds']
        return self._start('rescue', f'鲨总{seconds:g}秒后到，快帮她打掩护！',
                           ('装开会', '疯狂打字', '打小报告'), self.settings()['rescue_seconds'])

    @property
    def inspection_pending(self):
        return self._deferred_inspection or bool(self.round and self.round['kind'] == 'rescue')

    def start_task(self):
        if not self.settings()['tasks_enabled'] or self.engine.state['status'] != 'working':
            return False
        if self.settings()['career_enabled'] and self.career['boss_day'] == self.engine._today():
            title, choices, rewards = '今天你当老板，给鲨总派活', ('让鲨总修Bug', '让鲨总冲咖啡', '让鲨总写日报'), (100, 100, 100)
        else:
            title, choices, rewards = random.choice(TASKS)
        if self._start('task', title, choices, self.settings()['task_seconds'], rewards=list(rewards)):
            self.task_wait = 0
            return True
        return False

    def start_rules(self):
        if not self.settings()['rules_enabled'] or self.rule:
            return False
        return self._start('rules', '观众决定下一条员工守则', [r['title'] for r in RULES], 15)

    def _vote(self, event, choice):
        voter = str(event.get('uid') or event.get('name') or '匿名')
        votes = self.round['votes']
        if voter not in votes:
            if len(votes) >= 10000:
                return
            self.career['wallet'] = min(1_000_000, self.career['wallet'] + 1)
            self.engine._dirty = True
        votes[voter] = choice

    def handle(self, event, text):
        if self.round:
            choices = self.round['choices']
            normalized = [''.join(c.split()).lower() for c in choices]
            choice = (int(text) - 1 if text in ('1', '2', '3') else
                      (normalized.index(text) if text in normalized else None))
            if choice is not None and choice < len(choices):
                self._vote(event, choice)
                return True
        if text in ('鲨总派活', '派任务', '离谱需求'):
            if not self.start_task():
                self._notice('先处理眼前的事', '工作中且没有其他投票时可以请鲨总派活')
            return True
        if text in ('改员工守则', '守则投票'):
            if not self.start_rules():
                self._notice('守则暂时不变', '请等当前投票或守则结束')
            return True
        for prefix, action in (('兑换', self._buy), ('使用', self._use)):
            if text.startswith(prefix):
                item = next((key for key, (name, _) in ITEMS.items() if text[len(prefix):] == name), None)
                if item:
                    action(item)
                    return True
        return False

    def _buy(self, key):
        if not self.settings()['items_enabled']:
            self._notice('道具已关闭')
            return
        name, cost = ITEMS[key]
        if self.career['wallet'] < cost:
            self._notice('公共积分不足', f'参与投票获得积分；{name}需要{cost}分')
        elif self.career['items'][key] >= 9:
            self._notice('道具柜装不下了', '先使用已有道具')
        else:
            self.career['wallet'] -= cost
            self.career['items'][key] += 1
            self.engine._dirty = True
            self._notice('道具到账', f'{name} +1；发“使用{name}”生效')
            self._cue('item_bought', item=name)

    def _use(self, key):
        name = ITEMS[key][0]
        if not self.settings()['items_enabled'] or self.engine.state['status'] == 'offwork':
            return
        active = {'coffee': self.coffee_left > 0, 'shield': self.shield,
                  'keyboard': self.keyboard_left > 0, 'pillow': self.pillow_left > 0}[key]
        if active:
            self._notice('道具还在生效', f'{name}用完后再用下一份')
            return
        if self.career['items'][key] <= 0:
            self._notice('道具柜里没有了', f'先发“兑换{name}”')
            return
        self.career['items'][key] -= 1
        if key == 'coffee':
            self.coffee_left = 20
        elif key == 'shield':
            self.shield = True
            self.career['armed_shield'] = True
        elif key == 'keyboard':
            self.keyboard_left = 25
        elif key == 'pillow':
            self.pillow_left = 60
        self.engine._dirty = True
        self._notice('道具生效', name, 'approve')
        self._cue('item_used', item=name)

    def block_catch(self):
        if self.settings()['items_enabled'] and self.shield:
            self.shield = False
            self.career['armed_shield'] = False
            self.engine._dirty = True
            self._notice('挡箭牌挡住了罚款', '本轮仍然白干，免掉查岗扣薪', 'inspect')
            self._cue('shield_blocked')
            return True
        return False

    def slack_finished(self, caught):
        if not caught and self.pillow_left > 0 and self.settings()['items_enabled']:
            self.pillow_left = 0
            self._money(30)
            self._notice('带薪摸鱼成功', '枕头奖励 +30 工资', 'approve')
            self._cue('pillow_paid')

    def _finish_round(self):
        current, self.round = self.round, None
        counts = [sum(v == i for v in current['votes'].values()) for i in range(len(current['choices']))]
        choice = counts.index(max(counts)) if any(counts) else None
        if current['kind'] == 'rescue':
            helped = counts[0] + counts[1] > counts[2]
            self.engine._finish_slack(not helped)
            if helped:
                self.career['rescues'] += 1
                method = current['choices'][0 if counts[0] >= counts[1] else 1]
                self._notice('掩护成功！', f'{method}；掩护 {counts[0]+counts[1]} 票 / 举报 {counts[2]} 票', 'approve')
                self._cue('rescue_done', result=f'观众用{method}成功帮你打掩护')
                self._grant_xp(40)
            else:
                self._cue('rescue_done', result='没有躲过查岗，按实际扣薪结果回应')
        elif current['kind'] == 'task':
            reward = current['rewards'][choice] if choice is not None else 0
            self._notice('任务有结果了', (current['choices'][choice] if choice is not None else '无人接单，先记下需求') + f'，工资 {reward:+g}', 'approve' if reward > 0 else 'angry')
            self._money(reward)
            if reward > 0:
                self.career['tasks'] += 1
                self._grant_xp(80)
            self._cue('task_done', result=self.result['text'] if self.result else '')
        elif current['kind'] == 'rules' and choice is not None:
            rule = dict(RULES[choice])
            rule.update(left=float(self.settings()['rule_seconds']), broken=False)
            self.rule = rule
            self._notice('新员工守则生效', rule['title'], 'assign')
            self._cue('rule_start', rule=rule['title'])

    def on_speech(self, text):
        if not self.settings()['rules_enabled'] or not self.rule or self.rule['broken']:
            return
        kind = self.rule['id']
        violated = ((kind == 'no_boss' and any(w in text for w in ('老板', '鲨总')))
                    or (kind == 'short' and len(re.findall(r'\w', text)) > 5)
                    or (kind == 'no_tsundere' and any(w in text for w in ('哼', '才不是'))))
        if violated:
            self.rule['broken'] = True
            self._notice('说漏嘴，违反守则！', '本轮只罚一次，工资 -60', 'angry')
            self._money(-60)
            self._cue('rule_broken', rule=self.rule['title'])

    def finish_shift(self):
        if self.settings()['career_enabled']:
            self.career['shifts'] += 1
        self.round = self.rule = None
        self._deferred_inspection = False
        self._grant_xp(300)

    def new_day(self):
        self.round = self.rule = self.result = None
        self.task_wait = 0
        self._in_debt = False
        self._debt_rewarded = False
        self.shield = self.career['armed_shield']
        self._deferred_inspection = False
        self.coffee_left = self.keyboard_left = self.pillow_left = 0

    def tick(self, dt):
        self.elapsed += dt
        settings = self.settings()
        if self.rule and not settings['rules_enabled']:
            self.rule = None
        if not settings['rescue_enabled']:
            self._deferred_inspection = False
        if self.round and not settings[{'rescue': 'rescue_enabled', 'task': 'tasks_enabled', 'rules': 'rules_enabled'}[self.round['kind']]]:
            self.round = None
        if self._deferred_inspection and not self._busy() and self.engine.state['status'] == 'slacking':
            self._deferred_inspection = False
            self.begin_inspection()
        for attr in ('coffee_left', 'keyboard_left', 'pillow_left', 'pose_left'):
            setattr(self, attr, max(0, getattr(self, attr) - dt))
        if self.result:
            self.result['left'] -= dt
            if self.result['left'] <= 0:
                self.result = None
        if self.rule:
            self.rule['left'] -= dt
            if self.rule['left'] <= 0:
                rule, self.rule = self.rule, None
                if not rule['broken']:
                    self._notice('守则挑战完成', '没有说漏嘴，工资 +80', 'approve')
                    self._money(80)
                    self._grant_xp(20)
                    self._cue('rule_done')
        if self.round:
            self.round['left'] -= dt
            if self.round['left'] <= 0:
                self._finish_round()
        elif self.engine.state['status'] == 'working':
            self.task_wait += dt
            if self.task_wait >= self.settings()['task_interval_seconds']:
                self.start_task()

    def speed_bonus(self):
        return 1.5 if self.settings()['items_enabled'] and self.coffee_left > 0 else 0

    def snapshot(self):
        active = None
        if self.round:
            active = {k: v for k, v in self.round.items() if k not in ('votes', 'rewards')}
            active['counts'] = [sum(v == i for v in self.round['votes'].values()) for i in range(len(self.round['choices']))]
        pose = ('inspect' if active and active['kind'] == 'rescue' else
                'angry' if self.engine.state['status'] == 'caught' else
                'assign' if active and active['kind'] == 'task' else
                'inspect' if active and active['kind'] == 'rules' else
                self.pose if self.pose_left > 0 else '')
        rank = rank_name(self.career)
        if rank == '临时老板' and self.career['boss_day'] != self.engine._today():
            rank = '资深经理'
        return {'round': active, 'rule': dict(self.rule) if self.rule else None,
                'result': dict(self.result) if self.result else None,
                'rank': rank, 'xp': self.career['xp'], 'wallet': self.career['wallet'],
                'items': {ITEMS[k][0]: v for k, v in self.career['items'].items()},
                'coffee_left': self.coffee_left, 'keyboard_left': self.keyboard_left,
                'pillow_left': self.pillow_left, 'shield': self.shield,
                'story': '欠薪还债中' if self.engine.state['wage'] < 0 else '今天也要准时下班',
                'shifts': self.career['shifts'], 'boss_pose': pose}

    def context(self):
        snap = self.snapshot()
        text = (f"办公室进度：{snap['rank']}，经验 {snap['xp']}，公共积分 {snap['wallet']}，{snap['story']}。")
        if snap['round']:
            current = snap['round']
            text += f"正在进行：{current['title']}；剩 {math.ceil(current['left'])} 秒；"
            text += '；'.join(f'发{i+1}={name}（{current["counts"][i]}票）' for i, name in enumerate(current['choices']))
        if self.rule:
            text += f"当前必须遵守：{self.rule['title']}，剩{math.ceil(self.rule['left'])}秒。"
        if self.result:
            text += f"刚发生：{self.result['title']}，{self.result['text']}。"
        return text
