"""十一种摸鱼情景离线验证：事件轮换、弹幕挑战、查岗、结算图鉴、画面与过期台词。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import config
from live_wage import WageRenderer
from wage import WageEngine, WagePlayer, DEFAULTS
from wage_slack import FISH, INGREDIENTS, INTERACTIVE, SLACK_EVENTS, SLACK_BY_ID, clean_book


class SlackSceneTests(unittest.TestCase):
    def setUp(self):
        self.engine = WageEngine({**DEFAULTS, 'auto_slack': False, 'tasks_enabled': False,
                                  'catch_rate': 0, 'rescue_enabled': False})
        self.now = 100
        self.engine._last_tick = self.now

    def event(self, text):
        self.engine.on_event({'type': 'danmaku', 'uid': 42, 'name': '观众', 'text': text})

    def advance(self, seconds):
        for _ in range(seconds):
            self.now += 1
            self.engine.tick(self.now)

    def test_two_random_cycles_cover_every_event_without_adjacent_repeats(self):
        seen = []
        for _ in range(2 * len(SLACK_EVENTS)):
            self.engine._start_slack('working')
            seen.append(self.engine.snapshot()['slack_event']['id'])
            self.engine._finish_slack(False)
        self.assertEqual(set(seen[:len(SLACK_EVENTS)]), set(SLACK_BY_ID))
        self.assertEqual(set(seen[len(SLACK_EVENTS):]), set(SLACK_BY_ID))
        self.assertTrue(all(a != b for a, b in zip(seen, seen[1:])))

    def test_all_named_commands_choose_scene_and_do_not_overwrite_active_event(self):
        for event in SLACK_EVENTS:
            for command in event['commands']:
                self.event(command)
                state = self.engine.snapshot()
                self.assertEqual(state['slack_event']['id'], event['id'], command)
                self.assertEqual(state['status'], 'slacking')
                self.event('刷短视频')
                self.assertEqual(self.engine.snapshot()['slack_event']['id'], event['id'])
                self.engine._finish_slack(False)
        self.assertFalse(self.engine._start_slack('working', '../../fake'))
        self.engine.set_paused(True)
        self.event('看番')
        self.assertEqual(self.engine.state['status'], 'working')

    def test_snapshots_cannot_change_catalog_and_event_clears_after_work_resumes(self):
        self.event('偷吃零食')
        snap = self.engine.snapshot()
        snap['slack_event']['title'] = 'changed'
        self.assertEqual(self.engine.snapshot()['slack_event']['title'], '偷吃零食')
        self.advance(3)
        self.assertEqual(self.engine.snapshot()['slack_elapsed'], 3)
        self.engine.set_paused(True)
        self.engine._last_tick = self.now
        self.advance(100)
        self.assertEqual(self.engine.snapshot()['slack_elapsed'], 3)
        self.engine.set_paused(False)
        self.engine._last_tick = self.now
        self.advance(7)
        self.assertIsNone(self.engine.snapshot()['slack_event'])
        self.assertEqual(self.engine.take_cue()['slack_event']['id'], 'snack')

    def test_caught_retains_original_scene_and_penalty_is_applied_once(self):
        self.event('打个盹')
        self.event('老板来了')
        snap = self.engine.snapshot()
        self.assertEqual(snap['slack_event']['id'], 'nap')
        self.assertEqual(snap['wage'], -500)
        self.event('老板来了')
        self.assertEqual(self.engine.state['wage'], -500)
        caught_cue = next(cue for cue in self.engine._cues if cue['type'] == 'caught')
        self.assertEqual(caught_cue['slack_event']['id'], 'nap')
        self.advance(5)
        self.assertIsNone(self.engine.snapshot()['slack_event'])

    def test_meeting_rescue_vote_keeps_original_slack_scene(self):
        self.engine = WageEngine({**DEFAULTS, 'tasks_enabled': False, 'catch_rate': 0})
        self.engine._last_tick = self.now
        self.event('刷短视频')
        self.event('老板来了')
        self.assertEqual(self.engine.take_cue()['slack_event']['id'], 'shorts')
        self.event('装开会')
        self.assertEqual(self.engine.play.snapshot()['round']['counts'], [1, 0, 0])
        self.assertEqual(self.engine.snapshot()['slack_event']['id'], 'shorts')
        self.advance(10)
        cue = self.engine.take_cue()
        self.assertEqual(cue['type'], 'rescue_done')
        self.assertEqual(cue['slack_event']['id'], 'shorts')

    def test_screens_are_distinct_animate_and_freeze_during_pause(self):
        renderer = WageRenderer()
        screens = []
        for event in SLACK_EVENTS:
            self.engine._start_slack('working', event['id'])
            state = self.engine.snapshot()
            state.update(viewer=None, floaters=[], speech='', paused=True, slack_elapsed=1)
            first = renderer.render(state, 100, foreground=False)
            np.testing.assert_array_equal(first, renderer.render(state, 104, foreground=False))
            state['slack_elapsed'] = 4
            later = renderer.render(state, 104, foreground=False)
            crop = first[598:830, 76:716]
            self.assertFalse(np.array_equal(crop, later[598:830, 76:716]), event['id'])
            screens.append(crop.copy())
            self.engine._finish_slack(False)
        for i, screen in enumerate(screens):
            self.assertTrue(all(not np.array_equal(screen, other) for other in screens[i+1:]))


class SlackArcadeTests(unittest.TestCase):
    """弹幕挑战引擎的边界行为：四种互动、结算、图鉴存档、快照安全与暂停/查岗屏蔽。"""

    def setUp(self):
        self.settings = {**DEFAULTS, 'auto_slack': False, 'tasks_enabled': False,
                         'rules_enabled': False, 'catch_rate': 0, 'rescue_enabled': False,
                         'items_enabled': False, 'poll_enabled': False}
        self.engine = WageEngine(lambda: self.settings)
        self.now = 100.0
        self.engine._last_tick = self.now

    def send(self, text, uid=42, name=None):
        self.engine.on_event({'type': 'danmaku', 'uid': uid,
                              'name': name or f'观众{uid}', 'text': text})

    def advance(self, seconds):
        while seconds > 1e-9:
            step = min(1.0, seconds)
            self.now += step
            self.engine.tick(self.now)
            seconds -= step

    def current(self):
        return self.engine.arcade.active

    def start_game(self, bite=4.0):
        with patch('wage_slack.random.randrange', return_value=int(bite * 100)):
            self.assertTrue(self.engine._start_slack('danmaku', 'game'))

    def start_coffee(self, recipe=('加奶', '加糖', '加冰')):
        with patch('wage_slack.random.sample', return_value=list(recipe)):
            self.assertTrue(self.engine._start_slack('danmaku', 'coffee'))

    def wallet(self):
        return self.engine.state['career']['wallet']

    def book(self):
        return self.engine.state['slack_book']

    def test_interactive_scenes_run_at_least_18_seconds_and_normal_keeps_config(self):
        self.assertEqual(DEFAULTS['slack_seconds'], 10.0)
        self.engine._start_slack('danmaku', 'nap')
        self.assertEqual(self.engine._slack_total, 10.0)
        self.engine._finish_slack(False)
        self.settings['slack_seconds'] = 25.0
        self.engine._start_slack('danmaku', 'nap')
        self.assertEqual(self.engine._slack_total, 25.0)
        self.engine._finish_slack(False)
        self.settings['slack_seconds'] = 10.0
        for kind in INTERACTIVE:
            with self.subTest(kind=kind):
                self.assertTrue(self.engine._start_slack('danmaku', kind))
                self.assertGreaterEqual(self.engine._slack_total, 18.0)
                self.assertEqual(self.engine.snapshot()['slack_total'], self.engine._slack_total)
                self.assertIsNotNone(self.current())
                self.engine._finish_slack(False)

    def test_game_one_rod_per_viewer_bite_window_and_relay(self):
        self.start_game(4.0)
        self.assertEqual(self.current()['bite_at'], 4.0)
        self.advance(2)
        self.send('提竿', 1)
        self.assertFalse(self.current()['won'])
        self.assertIn('提早了', self.current()['feedback'])
        # 早提消耗了自己的尝试：咬钩窗口内再提也不成功。
        self.advance(2)
        self.send('提竿', 1)
        self.assertFalse(self.current()['won'])
        self.assertIn('每人一竿', self.current()['feedback'])
        # 其他人接力，在 bite_at 边界（elapsed==4.0 已进入窗口）成功。
        self.send('提竿', 2)
        self.assertTrue(self.current()['won'])
        self.assertEqual(self.current()['reward'], 3)
        self.assertIn(self.current()['loot'], FISH)
        # 成功后只待结算，其他人再提竿不改变结果。
        self.advance(1)
        self.send('提竿', 3)
        self.assertEqual(len(self.current()['actors']), 2)
        # 结束前2秒起窗口关闭。
        self.engine._finish_slack(False)
        self.start_game(4.0)
        self.advance(16)
        self.send('提竿', 4)
        self.assertFalse(self.current()['won'])
        self.assertIn('游走', self.current()['feedback'])
        snap = self.engine.snapshot()['slack_play']['active']
        self.assertIn('游走', snap['hint'])

    def test_game_bite_time_stays_within_three_to_six_seconds(self):
        for _ in range(30):
            self.engine._start_slack('danmaku', 'game')
            self.assertTrue(3.0 <= self.current()['bite_at'] <= 6.0)
            self.engine._finish_slack(False)

    def test_cat_three_purrs_with_two_second_per_person_cooldown(self):
        self.assertTrue(self.engine._start_slack('danmaku', 'cat'))
        self.advance(1)
        self.send('摸摸', 1)
        self.assertEqual(self.current()['progress'], 1)
        # 同一观众不足2秒的连点不计数。
        self.send('摸摸', 1)
        self.assertEqual(self.current()['progress'], 1)
        self.advance(1)
        self.send('摸摸', 1)
        self.assertEqual(self.current()['progress'], 1)
        # 恰好间隔2秒可以接力单人完成。
        self.advance(1)
        self.send('摸摸', 1)
        self.assertEqual(self.current()['progress'], 2)
        self.advance(2)
        self.send('摸摸', 1)
        self.assertTrue(self.current()['won'])
        self.assertEqual(self.current()['reward'], 2)
        self.send('摸摸', 7)
        self.assertEqual(self.current()['progress'], 3)

    def test_cat_three_different_viewers_win_immediately(self):
        self.engine._start_slack('danmaku', 'cat')
        self.advance(1)
        for uid in (1, 2, 3):
            self.send('摸摸', uid)
        self.assertTrue(self.current()['won'])
        self.assertEqual(len(self.current()['actors']), 3)

    def test_coffee_first_four_seconds_only_watch_then_hidden_sequenced_steps(self):
        self.start_coffee()
        self.advance(2)
        snap = self.engine.snapshot()['slack_play']['active']
        self.assertEqual(snap['phase'], 'watch')
        self.assertIn('加奶 → 加糖 → 加冰', snap['hint'])
        # 开场配方期间不能加料，也不消耗冷却。
        self.send('加奶', 1)
        self.assertEqual(self.current()['progress'], 0)
        self.assertIn('先记配方', self.current()['feedback'])
        self.assertEqual(len(self.current()['actors']), 0)
        self.advance(2)
        snap = self.engine.snapshot()['slack_play']['active']
        self.assertEqual(snap['phase'], 'input')
        self.assertNotIn('加奶 → 加糖 → 加冰', snap['hint'])
        # 每人1.5秒冷却：隔1秒被忽略，满1.5秒放行。
        self.send('加奶', 1)
        self.assertEqual(self.current()['progress'], 1)
        self.advance(1)
        self.send('加糖', 1)
        self.assertEqual(self.current()['progress'], 1)
        self.advance(0.5)
        self.send('加糖', 1)
        self.assertEqual(self.current()['progress'], 2)

    def test_coffee_wrong_ingredient_resets_progress(self):
        self.start_coffee()
        self.advance(4)
        self.send('加糖', 1)  # 第一步就错
        self.assertEqual(self.current()['progress'], 0)
        self.assertIn('加错了', self.current()['feedback'])
        self.send('加奶', 2)
        self.assertEqual(self.current()['progress'], 1)
        self.advance(2)
        self.send('加冰', 2)  # 第二步错（正确应为加糖）
        self.assertEqual(self.current()['progress'], 0)
        self.assertFalse(self.current()['won'])
        self.advance(2)
        self.send('加奶', 3)
        self.advance(2)
        self.send('加糖', 3)
        self.advance(2)
        self.send('加冰', 3)
        self.assertTrue(self.current()['won'])
        self.assertEqual(self.current()['reward'], 3)
        self.assertEqual(self.current()['loot'], '完美记忆咖啡')

    def test_treasure_single_vote_changeable_and_unique_top_opens(self):
        self.assertTrue(self.engine._start_slack('danmaku', 'treasure'))
        table = self.current()['loot_table']
        base = self.wallet()
        positive = next(i for i, (_, reward) in enumerate(table) if reward > 0)
        other = (positive + 1) % 3
        self.advance(1)
        self.send(f'抽屉{positive + 1}', 1)
        self.send(f'抽屉{other + 1}', 1)  # 同一人改票，不重复计数
        counts = self.engine.snapshot()['slack_play']['active']['counts']
        self.assertEqual(counts[other], 1)
        self.assertEqual(counts[positive], 0)
        for uid in (2, 3):
            self.send(f'抽屉{positive + 1}', uid)
        self.advance(1)
        self.engine._finish_slack(False)
        self.assertTrue(self.book()['unlocked'])
        self.assertEqual(self.wallet(), base + table[positive][1])  # 寻宝投票本身不加积分
        self.assertIn(table[positive][0], self.engine.arcade.result['text'])

    def test_treasure_ties_no_votes_and_old_report_pay_nothing(self):
        for case in ('tie', 'empty', 'report'):
            with self.subTest(case=case):
                engine = WageEngine(lambda: self.settings)
                engine._last_tick = self.now
                engine._start_slack('danmaku', 'treasure')
                table = engine.arcade.active['loot_table']
                zero = next(i for i, (_, reward) in enumerate(table) if reward == 0)
                other = next(i for i in range(3) if i != zero)
                if case == 'tie':
                    engine.on_event({'type': 'danmaku', 'uid': 1, 'text': f'抽屉{zero + 1}'})
                    engine.on_event({'type': 'danmaku', 'uid': 2, 'text': f'抽屉{other + 1}'})
                    engine._finish_slack(False)
                    self.assertIn('平票', engine.arcade.result['text'])
                elif case == 'empty':
                    engine._finish_slack(False)
                    self.assertIn('无人投票', engine.arcade.result['text'])
                else:
                    for uid in (1, 2, 3):
                        engine.on_event({'type': 'danmaku', 'uid': uid, 'text': f'抽屉{zero + 1}'})
                    engine._finish_slack(False)
                    self.assertIn('打开了旧周报', engine.arcade.result['text'])
                self.assertEqual(engine.state['career']['wallet'], 5)  # 初始公共积分不变
                self.assertEqual(engine.state['slack_book']['unlocked'], [])
                self.assertFalse(engine.arcade.result['success'])

    def test_safe_finish_grants_wallet_book_and_streak_bonus(self):
        self.start_game(4.0)
        self.advance(4)
        wage_before = self.engine.state['wage']
        base = self.wallet()
        self.send('提竿', 1)
        self.assertTrue(self.current()['won'])
        loot = self.current()['loot']
        # 成功后结算前不加任何奖励。
        self.assertEqual(self.wallet(), base)
        self.assertEqual(self.book()['unlocked'], [])
        self.advance(14)
        self.assertEqual(self.wallet(), base + 3)
        self.assertEqual(self.book()['unlocked'], ['game'])
        self.assertEqual(self.book()['fish'], [loot])
        self.assertEqual((self.book()['wins'], self.book()['streak'], self.book()['best_streak']), (1, 1, 1))
        self.assertEqual(self.engine.state['career']['xp'], 20)
        self.assertEqual(self.engine.state['wage'], wage_before)  # 公共积分不加工资
        self.assertTrue(self.engine.arcade.result['success'])
        self.assertIn('公共积分 +3', self.engine.arcade.result['text'])

    def test_every_third_consecutive_success_adds_three_public_points(self):
        self.book().update(wins=2, streak=2, best_streak=2)
        self.start_game(4.0)
        self.advance(4)
        self.send('提竿', 1)
        self.advance(14)
        self.assertEqual(self.book()['streak'], 3)
        self.assertEqual(self.wallet(), 5 + 3 + 3)  # 初始5 + 奖励3 + 连胜加成3
        self.assertIn('含连胜奖励 +3', self.engine.arcade.result['text'])

    def test_caught_confiscates_and_resets_streak(self):
        self.book().update(wins=4, streak=4, best_streak=4)
        self.start_game(4.0)
        self.advance(4)
        self.send('提竿', 1)
        self.send('老板来了')  # rescue关闭时直接抓包
        self.assertEqual(self.engine.state['status'], 'caught')
        self.assertEqual(self.wallet(), 5)  # 初始积分未被没收也未被奖励
        self.assertEqual(self.book()['unlocked'], [])
        self.assertEqual(self.book()['streak'], 0)
        self.assertIn('成果没收', self.engine.arcade.result['text'])
        self.assertIn('连胜归零', self.engine.arcade.result['text'])

    def test_shield_keeps_pending_reward_after_inspection(self):
        self.settings['items_enabled'] = True
        self.send('兑换挡箭牌')  # 花光初始5分
        self.send('使用挡箭牌')
        self.start_game(4.0)
        self.advance(4)
        self.send('提竿', 1)
        self.send('老板来了')
        self.assertEqual(self.engine.state['status'], 'working')
        self.assertEqual(self.engine.state['caught'], 0)
        self.assertEqual(self.wallet(), 3)  # 盾牌只免罚款，只发挑战奖励
        self.assertEqual(self.book()['unlocked'], ['game'])
        self.assertEqual(self.book()['streak'], 1)

    def test_normal_scene_safe_finish_unlocks_without_rewards(self):
        self.engine._start_slack('danmaku', 'daydream')
        self.advance(10)
        self.assertEqual(self.book()['unlocked'], ['daydream'])
        self.assertEqual(self.wallet(), 5)
        self.assertEqual(self.book()['wins'], 0)
        self.assertIsNone(self.engine.arcade.result)
        kinds = [cue['type'] for cue in self.engine._cues]
        self.assertIn('slack_done', kinds)
        self.assertNotIn('slack_game_done', kinds)

    def test_unfinished_challenge_and_duplicate_finish_pay_nothing(self):
        self.engine._start_slack('danmaku', 'cat')
        self.advance(1)
        self.send('摸摸', 1)
        self.advance(20)  # 只摸了1/3就超时
        self.assertEqual(self.wallet(), 5)  # 初始积分不动
        self.assertEqual(self.book()['unlocked'], [])
        self.assertFalse(self.engine.arcade.result['success'])
        self.assertIn('挑战未完成', self.engine.arcade.result['text'])
        wallet, wins = self.wallet(), self.book()['wins']
        self.engine._finish_slack(False)  # 已结束状态重复结算
        self.assertEqual((self.wallet(), self.book()['wins']), (wallet, wins))

    def test_double_finish_never_double_pays(self):
        self.start_game(4.0)
        self.advance(4)
        self.send('提竿', 1)
        self.advance(14)
        self.assertEqual(self.wallet(), 8)  # 初始5 + 只发一次的3
        self.engine._finish_slack(False)
        self.engine.arcade.finish(False, 'game')  # 直接重放结算也不能再领
        self.assertEqual(self.wallet(), 8)
        self.assertEqual(self.book()['wins'], 1)

    def test_pause_freezes_challenge_clock_and_result_window(self):
        self.start_game(4.0)
        self.advance(5)
        self.send('提竿', 1)
        left = self.engine.state['slack_left']
        self.engine.set_paused(True)
        self.engine._last_tick = self.now
        self.advance(30)
        self.assertEqual(self.engine.state['slack_left'], left)
        self.engine.set_paused(False)
        self.engine._last_tick = self.now
        self.advance(13)  # 恢复后从冻结处继续到18秒结算
        self.assertIsNotNone(self.engine.arcade.result)
        self.engine.set_paused(True)
        self.engine._last_tick = self.now
        self.advance(30)  # 暂停期间结果倒计时同样冻结
        self.assertIsNotNone(self.engine.snapshot()['slack_play']['result'])
        self.engine.set_paused(False)
        self.engine._last_tick = self.now
        self.advance(11)
        self.assertIsNone(self.engine.snapshot()['slack_play']['result'])

    def test_inspection_pending_blocks_challenge_actions_and_rescue_votes(self):
        self.settings['rescue_enabled'] = True
        self.start_game(4.0)
        self.advance(5)
        self.send('提竿', 1)
        self.assertTrue(self.current()['won'])
        self.send('老板来了')
        snap = self.engine.snapshot()['slack_play']['active']
        self.assertEqual(snap['phase'], 'inspection')
        self.send('提竿', 2)  # 查岗期间操作被屏蔽
        self.assertEqual(len(self.current()['actors']), 1)
        self.send('1', 5)  # 1/2/3 归救援投票，不被挑战抢走
        self.assertEqual(self.engine.play.round['votes'], {'5': 0})
        self.advance(10)
        self.assertEqual(self.engine.state['status'], 'working')
        self.assertEqual(self.wallet(), 5 + 1 + 3)  # 初始5 + 救援投票1 + 躲过查岗入袋3
        self.assertEqual(self.book()['unlocked'], ['game'])

    def test_snapshot_hides_secrets_and_cannot_mutate_engine(self):
        self.start_coffee()
        self.advance(5)
        self.send('加奶', 777001, name='绝不出现的名字')
        play = self.engine.snapshot()['slack_play']
        dump = json.dumps(play, ensure_ascii=False)
        self.assertNotIn('777001', dump)
        self.assertNotIn('绝不出现的名字', dump)
        self.assertNotIn('加奶 → 加糖 → 加冰', dump)
        for key in ('recipe', 'loot_table', 'actors', 'bite_at'):
            self.assertNotIn(key, dump)
        # 快照返回的是副本，改动不能回流到引擎数据。
        self.engine._finish_slack(False)  # 结束咖啡轮，才能开启钓鱼轮
        self.start_game(4.0)
        self.advance(4)
        self.send('提竿', 1)
        play = self.engine.snapshot()['slack_play']
        self.assertEqual(play['active']['phase'], 'ready')
        play['active']['progress'] = 99
        play['book']['unlocked'].append('伪造')
        play['book']['wins'] = 999
        self.assertIsNone(play['result'])
        fresh = self.engine.snapshot()['slack_play']
        self.assertEqual(fresh['active']['progress'], 1)
        self.assertEqual(fresh['book']['unlocked'], [])
        self.assertEqual(fresh['book']['wins'], 0)
        self.engine._finish_slack(False)
        self.assertIn('公共积分 +3', self.engine.arcade.result['text'])
        tampered = self.engine.snapshot()['slack_play']
        original = tampered['result']['text']
        tampered['result']['text'] = '改了'
        self.assertEqual(self.engine.snapshot()['slack_play']['result']['text'], original)
        # treasure 快照只暴露旧周报线索，不泄露其他抽屉内容。
        self.engine._start_slack('danmaku', 'treasure')
        table = self.current()['loot_table']
        zero_idx = next(i for i, (_, reward) in enumerate(table) if not reward)
        self.send(f'抽屉{zero_idx + 1}', 1)
        dump = json.dumps(self.engine.snapshot()['slack_play'], ensure_ascii=False)
        self.assertIn('旧周报', dump)
        for name, reward in table:
            if reward:
                self.assertNotIn(name, dump)

    def test_corrupt_and_legacy_saves_are_sanitized(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wage.json'
            today = self.engine._today()
            cases = [
                ({'unlocked': ['game', '../../evil', 'game', 7, None], 'fish': ['金色摸鱼王', '水怪'],
                  'wins': 'x', 'streak': -3, 'best_streak': float('nan')},
                 {'unlocked': ['game'], 'fish': ['金色摸鱼王'], 'wins': 0, 'streak': 0, 'best_streak': 0}),
                ('not-a-dict', clean_book(None)),
                (None, clean_book(None)),
            ]
            for raw_book, expected in cases:
                path.write_text(json.dumps({'date': today, 'wage': 500, 'slack_book': raw_book},
                                           ensure_ascii=False), encoding='utf8')
                loaded = WageEngine(lambda: self.settings, persist_path=str(path))
                self.assertEqual(loaded.state['slack_book'], expected)
                self.assertEqual(loaded.snapshot()['slack_play']['book']['unlocked'], expected['unlocked'])

    def test_book_survives_date_rollover_on_load_and_in_play(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wage.json'
            yesterday = '2020-01-01'
            book = {'unlocked': ['nap', 'game'], 'fish': ['锦鲤同事'], 'wins': 2, 'streak': 1, 'best_streak': 2}
            path.write_text(json.dumps({'date': yesterday, 'wage': 500, 'status': 'slacking',
                                       'slack_book': book}, ensure_ascii=False), encoding='utf8')
            loaded = WageEngine(lambda: self.settings, persist_path=str(path))
            state = loaded.snapshot()
            self.assertEqual(state['wage'], 0)
            self.assertEqual(state['status'], 'working')
            self.assertEqual(state['slack_book'], book)
        # 当天跨过午夜：图鉴与连胜保留，工资重置。
        self.engine._start_slack('danmaku', 'nap')
        self.advance(10)
        self.assertEqual(self.book()['unlocked'], ['nap'])
        with patch.object(self.engine, '_today', return_value='2099-01-02'):
            self.send('你好')
        state = self.engine.snapshot()
        self.assertEqual(state['slack_book']['unlocked'], ['nap'])
        self.assertEqual(state['wage'], 0)
        self.assertEqual(state['status'], 'working')
        self.assertIsNone(self.engine.arcade.active)

    def test_gift_reaching_target_cancels_pending_challenge_without_reward(self):
        self.settings['target'] = 100
        self.start_game(4.0)
        self.advance(4)
        self.send('提竿', 1)
        self.assertTrue(self.current()['won'])
        self.engine.on_event({'type': 'gift', 'value': 10, 'paid': True, 'gift_tid': 'tx-1'})
        self.assertEqual(self.engine.state['status'], 'offwork')
        self.assertIsNone(self.engine.arcade.active)
        self.assertIsNone(self.engine.arcade.result)
        self.assertEqual(self.wallet(), 5)  # 未结算挑战不发放公共积分
        self.assertEqual(self.book()['unlocked'], [])
        self.advance(30)
        self.assertEqual(self.wallet(), 5)
        self.send('提竿', 2)  # 下班后弹幕不再驱动挑战

    def test_pillow_reaching_target_keeps_settled_challenge_result(self):
        self.settings.update(target=100, items_enabled=True)
        self.engine.state['wage'] = 70
        self.engine.play.pillow_left = 60
        self.start_game(4.0)
        self.advance(4)
        self.send('提竿', 1)
        self.advance(14)
        self.assertEqual(self.engine.state['status'], 'offwork')
        self.assertEqual(self.wallet(), 8)
        self.assertTrue(self.engine.arcade.result['success'])
        self.assertIn('公共积分 +3', self.engine.arcade.result['text'])

    def test_item_notices_do_not_cover_running_challenge(self):
        self.engine._start_slack('danmaku', 'cat')
        self.engine.play._notice('道具到账', '咖啡 +1')
        state = self.engine.snapshot()
        renderer = WageRenderer()
        self.assertFalse(renderer._play_parts(state)[-1])
        self.assertFalse(any(layer.size == (676, 310) for layer, _, _ in renderer._office_layers(state)))
        self.send('摸摸', 1)
        self.assertEqual(self.current()['progress'], 1)
        self.advance(2)
        self.assertEqual(self.engine.state['slack_left'], 16)

    def test_existing_poll_suspends_time_and_inputs_then_resumes(self):
        self.settings['poll_enabled'] = True
        self.send('加薪投票')
        self.engine._start_slack('danmaku', 'cat')
        self.advance(8)
        self.send('摸摸', 1)
        self.assertEqual(self.engine.state['slack_left'], 18)
        self.assertEqual(self.current()['progress'], 0)
        self.send('1', 1)
        self.assertEqual(self.engine.snapshot()['poll']['yes'], 1)
        self.advance(28)
        self.advance(2)
        self.send('摸摸', 1)
        self.assertLess(self.engine.state['slack_left'], 18)
        self.assertEqual(self.current()['progress'], 1)

    def test_treasure_votes_are_resolved_only_on_safe_finish_when_caught(self):
        self.engine._start_slack('danmaku', 'treasure')
        for uid in (1, 2, 3):
            self.send('抽屉2', uid)
        self.send('老板来了')
        self.assertEqual(self.engine.state['status'], 'caught')
        self.assertEqual(self.wallet(), 5)
        self.assertEqual(self.book()['unlocked'], [])
        self.assertEqual(self.book()['streak'], 0)


class SlackArcadeCueTests(unittest.IsolatedAsyncioTestCase):
    """新结果AI cue slack_game_done：只播当前一轮且结果未过期的台词。"""

    def fixture(self):
        settings = {**DEFAULTS, 'auto_slack': False, 'catch_rate': 0, 'rescue_enabled': False,
                    'tasks_enabled': False, 'rules_enabled': False, 'items_enabled': False}
        engine = WageEngine(lambda: settings)
        engine._last_tick = 100.0
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), wage=engine,
                              brain=SimpleNamespace(ai=AsyncMock(return_value=('鱼保住啦', '开心'))),
                              pipe=SimpleNamespace(react=Mock(), narrate_fresh=Mock()), push=Mock())
        return app, WagePlayer(app, DEFAULTS), engine, settings

    def win_and_finish(self, engine):
        with patch('wage_slack.random.randrange', return_value=400):
            engine._start_slack('danmaku', 'game')
        now = 100.0
        for _ in range(4):
            now += 1
            engine.tick(now)
        engine.on_event({'type': 'danmaku', 'uid': 1, 'text': '提竿'})
        for _ in range(14):
            now += 1
            engine.tick(now)
        engine._last_tick = now
        cue = next(c for c in engine._cues if c['type'] == 'slack_game_done')
        return cue

    async def test_cue_is_emitted_with_result_and_current_seq(self):
        app, player, engine, _ = self.fixture()
        cue = self.win_and_finish(engine)
        self.assertEqual(cue['slack_seq'], engine._slack_seq)
        self.assertEqual(cue['result'], engine.arcade.result['text'])
        self.assertTrue(await player._speak(cue, force=True))
        app.pipe.narrate_fresh.assert_called_once()

    async def test_expired_switched_round_and_paused_cues_are_dropped(self):
        for case in ('expired', 'switched', 'paused'):
            with self.subTest(case=case):
                app, player, engine, _ = self.fixture()
                cue = self.win_and_finish(engine)
                now = engine._last_tick
                if case == 'expired':
                    for _ in range(11):
                        now += 1
                        engine.tick(now)
                    self.assertIsNone(engine.arcade.result)
                elif case == 'switched':
                    engine._start_slack('working', 'nap')
                else:
                    engine.set_paused(True)
                self.assertFalse(await player._speak(cue, force=True))
                app.pipe.narrate_fresh.assert_not_called()
                app.pipe.react.assert_not_called()

    async def test_result_replaced_by_book_while_ai_runs_is_not_spoken(self):
        app, player, engine, _ = self.fixture()
        cue = self.win_and_finish(engine)
        async def delayed(*args):
            engine.on_event({'type': 'danmaku', 'text': '摸鱼图鉴'})
            return '鱼保住啦', '开心'
        app.brain.ai = AsyncMock(side_effect=delayed)
        self.assertFalse(await player._speak(cue, force=True))
        app.pipe.narrate_fresh.assert_not_called()

    async def test_normal_scene_never_emits_game_done_cue(self):
        app, player, engine, _ = self.fixture()
        engine._start_slack('working', 'nap')
        now = 100.0
        for _ in range(10):
            now += 1
            engine.tick(now)
        kinds = [cue['type'] for cue in engine._cues]
        self.assertNotIn('slack_game_done', kinds)


class SlackSpeechTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), wage=WageEngine(DEFAULTS),
                              brain=SimpleNamespace(ai=AsyncMock(return_value=('再看一个就回去上班。', '开心'))),
                              pipe=SimpleNamespace(react=Mock(), narrate_fresh=Mock()), push=Mock())
        return app, WagePlayer(app, DEFAULTS)

    async def test_auto_and_danmaku_context_include_the_selected_activity(self):
        app, player = self.fixture()
        app.wage._start_slack('working', 'shopping')
        cue = app.wage.take_cue()
        prompt = player._prompt(cue)
        self.assertIn('逛购物车', prompt)
        self.assertIn('你自己忍不住', prompt)
        self.assertIn('逛购物车', player.commentary_context())
        self.assertTrue(await player._speak(cue, force=True))
        app.pipe.react.assert_called_once()

    async def test_late_ai_response_does_not_describe_a_finished_or_replaced_event(self):
        for replacement in (None, 'nap'):
            with self.subTest(replacement=replacement):
                app, player = self.fixture()
                app.wage._start_slack('working', 'shorts')
                cue = app.wage.take_cue()
                async def delayed(*args):
                    app.wage._finish_slack(False)
                    if replacement:
                        app.wage._start_slack('working', replacement)
                    return '再刷一个就回去！', '开心'
                app.brain.ai = AsyncMock(side_effect=delayed)
                self.assertFalse(await player._speak(cue, force=True))
                app.pipe.react.assert_not_called()
                app.pipe.narrate_fresh.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
