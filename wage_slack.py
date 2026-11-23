"""摸鱼小剧场与弹幕挑战；不访问网站、不执行观众输入。"""
import copy
import math
import random

SLACK_EVENTS = (
    {'id': 'anime', 'title': '偷偷看番', 'label': '偷偷看番中',
     'scene': '把工作屏幕切成番剧小剧场，追到精彩处还在装镇定',
     'excuse': '这是研究角色动作与用户体验', 'accent': (164, 158, 250),
     'commands': ('看番', '偷偷看番', '追番')},
    {'id': 'shorts', 'title': '刷短视频', 'label': '刷视频摸鱼',
     'scene': '划短视频停不下来，刚说完最后一个又划到下一个',
     'excuse': '这是调研用户注意力，不是沉迷小视频', 'accent': (250, 119, 173),
     'commands': ('刷短视频', '刷视频', '摸鱼刷视频')},
    {'id': 'shopping', 'title': '逛购物车', 'label': '偷偷逛街中',
     'scene': '偷偷把零食、键盘和鱼抱枕加进购物车，工资还没到就开始精打细算',
     'excuse': '只是采购办公用品，结算按钮还没碰', 'accent': (246, 190, 94),
     'commands': ('逛购物车', '逛网店', '摸鱼购物')},
    {'id': 'snack', 'title': '偷吃零食', 'label': '零食补给中',
     'scene': '趁鲨总不注意拆开零食，努力把包装袋捏得安静一点',
     'excuse': '这是补充脑力，薯片也是算力耗材', 'accent': (245, 171, 116),
     'commands': ('偷吃零食', '吃零食', '吃薯片')},
    {'id': 'meeting', 'title': '假装开会', 'label': '假装开会中',
     'scene': '打开四个人的假会议窗口，自己一本正经点头，实际上并没有会议',
     'excuse': '正在跟空气对齐需求，不能随便打断', 'accent': (106, 211, 194),
     'commands': ('假装开会', '装开会')},
    {'id': 'nap', 'title': '偷偷打盹', 'label': '偷偷打盹中',
     'scene': '低头闭眼打一个小盹，屏幕已经悄悄换成月亮屏保',
     'excuse': '这是闭眼编译，正在脑内跑测试', 'accent': (170, 175, 245),
     'commands': ('打个盹', '偷偷打盹', '打盹', '偷睡')},
    {'id': 'game', 'title': '偷玩小游戏', 'label': '偷玩游戏中',
     'scene': '观众等水面出现咬钩提示再发提竿，每人一竿，安全结束后鱼获才会入袋',
     'excuse': '正在测试交互反馈，这条鱼是测试用例', 'accent': (114, 211, 244),
     'commands': ('偷玩游戏', '打小游戏', '摸鱼钓鱼')},
    {'id': 'daydream', 'title': '工位发呆', 'label': '工位放空中',
     'scene': '看着窗外放空，脑海里只有云朵和一条缓缓游过的小鱼',
     'excuse': '这是后台思考，表面没动其实还在加载', 'accent': (145, 217, 228),
     'commands': ('发呆', '工位发呆', '摸鱼发呆')},
    {'id': 'cat', 'title': '工位撸猫', 'label': '偷偷撸猫中',
     'scene': '一只猫占领了键盘，观众发摸摸攒满三次呼噜，同一个人隔两秒也能接力',
     'excuse': '这是给键盘做毛绒压力测试', 'accent': (248, 190, 139),
     'commands': ('工位撸猫', '撸猫', '摸鱼撸猫')},
    {'id': 'coffee', 'title': '记忆咖啡', 'label': '偷偷冲咖啡',
     'scene': '先记住四秒内展示的三步配方，再用弹幕按顺序加料，放错就从头再来',
     'excuse': '这是办公室液态算力升级', 'accent': (227, 196, 151),
     'commands': ('冲咖啡', '记忆咖啡', '摸鱼咖啡')},
    {'id': 'treasure', 'title': '抽屉寻宝', 'label': '翻抽屉寻宝',
     'scene': '三个抽屉藏着金色工牌、咖啡券或旧周报，观众根据线索投票决定打开哪一个',
     'excuse': '这是在盘点公司的固定资产', 'accent': (245, 216, 116),
     'commands': ('抽屉寻宝', '摸鱼寻宝', '翻抽屉')},
)

SLACK_BY_ID = {event['id']: event for event in SLACK_EVENTS}
SLACK_COMMANDS = {command: event['id'] for event in SLACK_EVENTS for command in event['commands']}


def event_view(event):
    """快照和 AI cue 使用独立数据，不允许调用方改动目录。"""
    return {key: value for key, value in event.items() if key != 'commands'} if event else None


INTERACTIVE = ('game', 'cat', 'coffee', 'treasure')
FISH = ('加班咸鱼', '锦鲤同事', '金色摸鱼王')
INGREDIENTS = ('加奶', '加糖', '加冰', '加浓')
DRAWERS = ('抽屉1', '抽屉2', '抽屉3')


def book_defaults():
    return {'unlocked': [], 'fish': [], 'wins': 0, 'streak': 0, 'best_streak': 0}


def clean_book(raw):
    result = book_defaults()
    if not isinstance(raw, dict):
        return result
    for key, allowed in (('unlocked', SLACK_BY_ID), ('fish', FISH)):
        values = raw.get(key)
        if isinstance(values, list):
            result[key] = [name for name in allowed if name in values]
    for key in ('wins', 'streak', 'best_streak'):
        value = raw.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            result[key] = max(0, min(1_000_000_000, int(value)))
    result['streak'] = min(result['streak'], result['wins'])
    result['best_streak'] = min(result['wins'], max(result['streak'], result['best_streak']))
    return result


class SlackArcade:
    def __init__(self, engine):
        self.engine = engine
        self.active = None
        self.result = None

    @property
    def book(self):
        return self.engine.state['slack_book']

    @property
    def elapsed(self):
        return max(0, self.engine._slack_total - self.engine.state['slack_left'])

    @property
    def suspended(self):
        return bool(self.active and (self.engine.play.inspection_pending or self.engine.play._busy()))

    def start(self, kind):
        self.active = None
        self.result = None
        if kind not in INTERACTIVE:
            return
        self.active = {'kind': kind, 'progress': 0, 'goal': 3 if kind in ('cat', 'coffee') else 1,
                       'actors': {}, 'feedback': '', 'won': False, 'reward': 0, 'loot': ''}
        if kind == 'game':
            self.active['bite_at'] = random.randrange(300, 601) / 100
        elif kind == 'coffee':
            self.active['recipe'] = random.sample(INGREDIENTS, 3)
        elif kind == 'treasure':
            loot = [('金色工牌', 5), ('咖啡券', 3), ('旧周报', 0)]
            random.shuffle(loot)
            self.active['loot_table'] = loot

    def tick(self, dt):
        if self.result:
            self.result['left'] -= dt
            if self.result['left'] <= 0:
                self.result = None

    def cancel(self):
        self.active = None
        self.result = None

    def handle(self, event, text):
        if text == '摸鱼图鉴':
            if not self.active:
                names = '、'.join(SLACK_BY_ID[key]['title'] for key in self.book['unlocked']) or '还没有点亮的场景'
                fish = '、'.join(self.book['fish']) or '暂无鱼获'
                self.result = {'title': f'摸鱼图鉴 {len(self.book["unlocked"])}/{len(SLACK_EVENTS)}',
                               'text': f'{names}；鱼获：{fish}；最高连胜 {self.book["best_streak"]}',
                               'left': 12, 'success': True}
            return True
        current = self.active
        if not current or self.engine.state['status'] != 'slacking':
            return False
        kind = current['kind']
        commands = {'game': ('提竿',), 'cat': ('摸摸',), 'coffee': INGREDIENTS, 'treasure': DRAWERS}[kind]
        if text not in commands:
            return False
        if self.suspended or current['won']:
            return True
        elapsed = self.elapsed
        if kind == 'coffee' and elapsed < 4:
            current['feedback'] = '先记配方，收起后再加料'
            return True
        voter = str(event.get('uid') or event.get('name') or '匿名')[:160]
        actors = current['actors']
        if voter not in actors and len(actors) >= 1000:
            return True
        previous = actors.get(voter)
        if kind == 'game' and previous is not None:
            current['feedback'] = '每人一竿，换位观众接力吧'
            return True
        if kind in ('cat', 'coffee') and previous is not None and elapsed - previous < (2 if kind == 'cat' else 1.5):
            return True
        actors[voter] = DRAWERS.index(text) if kind == 'treasure' else elapsed
        if kind == 'game':
            if current['bite_at'] <= elapsed < self.engine._slack_total - 2:
                current.update(won=True, progress=1, reward=3, loot=random.choices(FISH, weights=(6, 3, 1))[0])
                current['feedback'] = f'{current["loot"]}上钩！躲过查岗才入袋'
            else:
                current['feedback'] = '提早了，鱼还没咬钩' if elapsed < current['bite_at'] else '鱼游走了，下轮再来'
        elif kind == 'cat':
            current['progress'] += 1
            current['feedback'] = f'呼噜值 {current["progress"]}/3，同一观众隔2秒可再摸'
            if current['progress'] == 3:
                current.update(won=True, reward=2, loot='猫猫的信任', feedback='猫猫睡着了，等安全收工！')
        elif kind == 'coffee':
            if text == current['recipe'][current['progress']]:
                current['progress'] += 1
                current['feedback'] = f'顺序正确，已完成 {current["progress"]}/3 步'
            else:
                current['progress'] = 0
                current['feedback'] = '加错了！从记住的第一步重新来'
            if current['progress'] == 3:
                current.update(won=True, reward=3, loot='完美记忆咖啡', feedback='配方完成！等安全收工')
        else:
            current['feedback'] = '已投票，可改票；结算时唯一最高票抽屉打开'
        return True

    def finish(self, caught, event_id):
        current, self.active = self.active, None
        if current and current['kind'] == 'treasure' and not caught:
            counts = [sum(v == i for v in current['actors'].values()) for i in range(3)]
            if max(counts) > 0 and counts.count(max(counts)) == 1:
                loot, reward = current['loot_table'][counts.index(max(counts))]
                current.update(won=reward > 0, loot=loot, reward=reward)
                current['feedback'] = f'打开了{loot}'
            else:
                current['feedback'] = '平票，抽屉保持关闭' if any(counts) else '无人投票，抽屉保持关闭'
        success = not caught and (not current or current['won'])
        if success and event_id not in self.book['unlocked']:
            self.book['unlocked'].append(event_id)
        if current:
            if success:
                self.book['wins'] += 1
                self.book['streak'] += 1
                self.book['best_streak'] = max(self.book['best_streak'], self.book['streak'])
                bonus = 3 if self.book['streak'] % 3 == 0 else 0
                points = current['reward'] + bonus
                self.engine.state['career']['wallet'] = min(1_000_000, self.engine.state['career']['wallet'] + points)
                if current['kind'] == 'game' and current['loot'] not in self.book['fish']:
                    self.book['fish'].append(current['loot'])
                text = f'{current["loot"]} · 公共积分 +{points} · {self.book["streak"]} 连胜'
                if bonus:
                    text += '（含连胜奖励 +3）'
                self.result = {'title': '摸鱼挑战成功', 'text': text, 'left': 10, 'success': True}
                self.engine.play._grant_xp(20)
            else:
                self.book['streak'] = 0
                text = ('被鲨总抓到了，本轮成果没收' if caught else
                        current['feedback'] if current['kind'] == 'treasure' else '时间到了，挑战未完成')
                self.result = {'title': '下次再挑战', 'text': text + ' · 无奖励，连胜归零', 'left': 10, 'success': False}
            self.engine._cue('slack_game_done', result=self.result['text'], slack_seq=self.engine._slack_seq)
        self.engine._dirty = True

    def snapshot(self):
        active = None
        current = self.active
        if current:
            kind, elapsed = current['kind'], self.elapsed
            phase = 'input'
            choices, counts = [], []
            if kind == 'game':
                phase = 'waiting' if elapsed < current['bite_at'] else 'bite' if elapsed < self.engine._slack_total - 2 else 'waiting'
                hint = ('鱼还没咬钩，先别提竿' if elapsed < current['bite_at'] else
                        '鱼咬钩了！发「提竿」，每人一竿' if phase == 'bite' else '鱼已经游走，下轮再来')
            elif kind == 'cat':
                hint = '发「摸摸」攒满3次呼噜；同一人每2秒可摸一次'
            elif kind == 'coffee':
                phase = 'watch' if elapsed < 4 else 'input'
                hint = ('记住顺序：' + ' → '.join(current['recipe']) if phase == 'watch' else
                        '配方收起了！按刚才的顺序发弹幕加料')
                choices = list(INGREDIENTS)
            else:
                empty = next(i for i, (_, reward) in enumerate(current['loot_table']) if not reward)
                hint = f'线索：{empty + 1}号只有旧周报；发抽屉1/2/3，平票不开'
                choices = list(DRAWERS)
                counts = [sum(v == i for v in current['actors'].values()) for i in range(3)]
            if current['won']:
                phase, hint = 'ready', '挑战完成，躲过查岗并结束本轮才领奖'
            if self.suspended:
                phase, hint = 'waiting', '挑战暂停，先完成当前投票'
            if self.engine.play.inspection_pending:
                phase, hint = 'inspection', '查岗已触发，先完成当前投票，再帮忙打掩护'
                if self.engine.play.round and self.engine.play.round['kind'] == 'rescue':
                    hint = '挑战暂停，先发1/2帮忙打掩护；发3是举报'
            active = {'kind': kind, 'title': SLACK_BY_ID[kind]['title'], 'hint': hint,
                      'feedback': current['feedback'], 'progress': current['progress'], 'goal': current['goal'],
                      'phase': phase, 'choices': choices, 'counts': counts, 'left': self.engine.state['slack_left']}
        return {'active': active, 'result': dict(self.result) if self.result else None,
                'book': {**copy.deepcopy(self.book), 'total': len(SLACK_EVENTS), 'fish_total': len(FISH),
                         'unlocked_titles': [SLACK_BY_ID[key]['title'] for key in self.book['unlocked']]}}

    def context(self):
        snap = self.snapshot()
        text = f'摸鱼图鉴 {len(self.book["unlocked"])}/{len(SLACK_EVENTS)}；挑战连胜 {self.book["streak"]}。公共积分用于道具柜，不是工资。'
        if snap['active']:
            active = snap['active']
            text += f'当前弹幕挑战：{active["hint"]}；进度 {active["progress"]}/{active["goal"]}。{active["feedback"]}。'
            text += '游戏成果仅为待结算，不能声称已经加分；查岗优先，不能凭聊天宣布成功。'
        if self.result:
            text += f'刚刚的挑战结果：{self.result["text"]}。'
        return text
