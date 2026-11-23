"""办公室玩法离线验收：真实事件入口、帧时钟、播音回调和临时存档。"""
import copy
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image
import config
from wage import WageEngine, WagePlayer, DEFAULTS
from wage_play import RULES, TASKS
from live_wage import WageRenderer


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.settings = {**DEFAULTS, 'base_minutes': 240, 'tasks_enabled': False,
                         'auto_slack': False, 'catch_rate': 0, 'speed_decay_per_second': 0}
        self.engine = WageEngine(lambda: self.settings)
        self.now = 100.0
        self.engine._last_tick = self.now

    def event(self, text, uid=42):
        self.engine.on_event({'type': 'danmaku', 'uid': uid, 'name': f'观众{uid}', 'text': text})

    def advance(self, seconds):
        while seconds > 0:
            step = min(1, seconds)
            self.now += step
            self.engine.tick(self.now)
            seconds -= step

    def inspection(self):
        self.event('摸鱼')
        self.event('老板来了')

    def test_rescue_warning_votes_unique_credit_and_escape(self):
        self.inspection()
        self.assertEqual(self.engine.snapshot()['office']['round']['kind'], 'rescue')
        self.event('1')
        self.event('2')  # 同一观众改票，不能重复加公共积分。
        self.event('装开会', 43)
        self.event('打小报告', 44)
        self.assertEqual(self.engine.state['career']['wallet'], 8)
        self.advance(9)
        self.assertEqual(self.engine.state['slack_left'], self.engine._slack_total)
        self.advance(1)
        state = self.engine.snapshot()
        self.assertEqual((state['status'], state['caught'], state['white'], state['wage']), ('working', 0, 1, 0))
        self.assertEqual(state['career']['rescues'], 1)
        self.assertIn('掩护成功', state['office']['result']['title'])

    def test_no_help_and_report_majority_are_caught(self):
        self.inspection()
        self.event('3')
        self.advance(10)
        state = self.engine.snapshot()
        self.assertEqual((state['status'], state['caught'], state['wage']), ('caught', 1, -500))
        self.assertEqual(state['office']['boss_pose'], 'angry')
        self.assertEqual(state['career']['debt_peak'], 500)

    def test_existing_salary_poll_defers_inspection_without_losing_votes(self):
        self.event('加薪投票')
        self.event('1')
        self.inspection()
        self.assertEqual(self.engine.snapshot()['poll']['yes'], 1)
        self.advance(37)
        self.assertEqual(self.engine.snapshot()['office']['round']['kind'], 'rescue')
        self.event('装开会')
        self.advance(10)
        self.assertEqual(self.engine.state['caught'], 0)
        self.assertEqual(self.engine.state['status'], 'working')

    def test_task_choices_pause_then_reward_and_rank(self):
        self.settings['tasks_enabled'] = True
        self.engine.state['career']['xp'] = 290
        with patch('wage_play.random.choice', return_value=TASKS[0]):
            self.event('鲨总派活')
        self.event('2')
        self.event('1')
        self.assertEqual(self.engine.state['career']['wallet'], 6)
        self.engine.set_paused(True)
        self.advance(50)
        self.assertEqual(self.engine.snapshot()['office']['round']['left'], 35)
        self.engine.set_paused(False)
        self.engine._last_tick = self.now
        self.advance(35)
        state = self.engine.snapshot()
        self.assertEqual(state['wage'], 160)
        self.assertEqual(state['career']['tasks'], 1)
        self.assertEqual(state['office']['rank'], '正式员工')
        self.assertEqual(state['office']['boss_pose'], 'approve')

    def test_automatic_tasks_and_empty_round_do_not_deduct_salary(self):
        self.settings.update(tasks_enabled=True, task_interval_seconds=30)
        self.advance(30)
        self.assertEqual(self.engine.snapshot()['office']['round']['kind'], 'task')
        self.advance(35)
        self.assertEqual(self.engine.state['wage'], 0)
        self.assertIsNone(self.engine.play.round)

    def test_public_items_transaction_and_coffee_cannot_stack_or_reset(self):
        self.event('兑换咖啡')
        self.event('使用咖啡')
        self.assertEqual(self.engine.state['career']['wallet'], 2)
        self.assertEqual(self.engine.snapshot()['speed'], 2.5)
        self.advance(5)
        self.event('使用咖啡')
        self.assertEqual(self.engine.play.coffee_left, 15)
        self.event('兑换挡箭牌')
        self.assertEqual(self.engine.state['career']['wallet'], 2)
        self.assertEqual(self.engine.state['career']['items']['shield'], 0)
        self.advance(15)
        self.assertEqual(self.engine.snapshot()['speed'], 1)

    def test_shield_is_consumed_once_and_pillow_pays_only_after_safe_slack(self):
        self.event('兑换挡箭牌')
        self.event('使用挡箭牌')
        self.inspection()
        self.advance(10)
        self.assertEqual(self.engine.state['wage'], 0)
        self.assertFalse(self.engine.play.shield)
        self.assertEqual(self.engine.state['caught'], 0)
        self.engine.state['career']['wallet'] = 3
        self.event('兑换枕头')
        self.event('使用枕头')
        self.event('摸鱼')
        self.advance(self.engine._slack_total)
        self.assertEqual(self.engine.state['wage'], 30)
        self.assertEqual(self.engine.play.pillow_left, 0)

    def test_rule_checks_spoken_text_once_instead_of_viewer_messages(self):
        self.event('改员工守则')
        self.event('1')
        self.advance(15)
        self.event('老板你快看我')
        self.assertEqual(self.engine.state['wage'], 0)
        self.engine._last_frame = time.monotonic()
        self.engine.on_speak('speak', {'text': '老板别看我呀'})
        self.engine.on_speak('speak', {'text': '老板我错了'})
        self.assertEqual(self.engine.state['wage'], -60)
        self.advance(30)
        self.assertIsNone(self.engine.play.rule)
        self.assertEqual(self.engine.state['wage'], -60)

    def test_rule_success_and_short_rule_ignores_punctuation(self):
        self.event('改员工守则')
        self.event('2')
        self.advance(15)
        self.engine._last_frame = time.monotonic()
        self.engine.on_speak('speak', {'text': '好，马上来！'})
        self.assertFalse(self.engine.play.rule['broken'])
        self.advance(30)
        self.assertEqual(self.engine.state['wage'], 80)
        self.engine.play.rule = {**RULES[1], 'left': 10, 'broken': False}
        self.engine._last_frame = time.monotonic()
        self.engine.on_speak('speak', {'text': '今天我真的不想再上班了'})
        self.assertEqual(self.engine.state['wage'], 20)

    def test_career_inventory_and_debt_survive_new_day_and_reload(self):
        self.engine.state['career'].update(xp=910, wallet=9)
        self.engine.state['career']['items']['keyboard'] = 2
        self.engine.state['career']['armed_shield'] = True
        self.engine.state['wage'] = -500
        self.engine.play.on_balance()
        self.engine.play._money(500)
        self.assertEqual(self.engine.state['career']['debt_clears'], 1)
        with tempfile.TemporaryDirectory() as folder:
            self.engine.persist_path = str(Path(folder) / 'office.json')
            self.engine.save()
            with patch('wage.WageEngine._today', return_value='2099-01-02'):
                loaded = WageEngine(self.settings, persist_path=self.engine.persist_path)
            self.assertEqual(loaded.state['wage'], 0)
            self.assertEqual(loaded.state['career']['xp'], 1010)
            self.assertEqual(loaded.state['career']['items']['keyboard'], 2)
            self.assertEqual(loaded.state['career']['wallet'], 9)
            self.assertEqual(loaded.state['career']['debt_clears'], 1)
            self.assertTrue(loaded.play.shield)

    def test_top_rank_is_boss_only_on_promotion_day(self):
        self.engine.state['career']['xp'] = 2990
        self.engine.play._grant_xp(10)
        self.assertEqual(self.engine.snapshot()['office']['rank'], '临时老板')
        self.settings['tasks_enabled'] = True
        self.event('鲨总派活')
        self.assertIn('给鲨总派活', self.engine.play.round['title'])
        with patch.object(self.engine, '_today', return_value='2099-01-03'):
            self.assertEqual(self.engine.snapshot()['office']['rank'], '资深经理')

    def test_feature_switches_cancel_active_effects_and_legacy_catch_still_works(self):
        self.event('改员工守则')
        self.settings['rules_enabled'] = False
        self.advance(1)
        self.assertIsNone(self.engine.play.round)
        self.settings['items_enabled'] = False
        self.event('兑换咖啡')
        self.assertEqual(self.engine.state['career']['wallet'], 5)
        self.settings['rescue_enabled'] = False
        self.inspection()
        self.assertEqual(self.engine.state['caught'], 1)

    def test_all_boss_poses_have_frames_and_office_render_changes(self):
        for pose in ('inspect', 'assign', 'angry', 'approve'):
            with Image.open(Path(config.WEB_DIR) / 'assets' / f'shark-boss-{pose}.gif') as gif:
                self.assertGreater(gif.n_frames, 20)
                self.assertEqual(gif.convert('RGBA').getchannel('A').getextrema(), (0, 255))
        renderer = WageRenderer()
        self.inspection()
        snap = self.engine.snapshot()
        frame = renderer.render(snap)
        self.assertEqual(renderer._boss_mode, 'inspect')
        self.assertEqual(frame.shape, (1080, 1920, 4))
        snap['office']['boss_pose'] = 'approve'
        other = renderer.render(snap)
        self.assertFalse(np.array_equal(frame, other))


class OfficeSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_rescue_warning_is_not_spoken_after_model_returns(self):
        engine = WageEngine({**DEFAULTS, 'auto_slack': False})
        engine.on_event({'type': 'danmaku', 'text': '摸鱼'})
        engine.on_event({'type': 'danmaku', 'text': '老板来了'})
        async def delayed(*args):
            engine.play._finish_round()
            return '鲨总要来了，快掩护我', '惊讶'
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), wage=engine,
                              brain=SimpleNamespace(ai=AsyncMock(side_effect=delayed)),
                              pipe=SimpleNamespace(react=Mock(), narrate_fresh=Mock()))
        player = WagePlayer(app, DEFAULTS)
        self.assertFalse(await player._speak({'type': 'rescue_start'}, force=True))
        app.pipe.narrate_fresh.assert_not_called()
        app.pipe.react.assert_not_called()

    async def test_task_start_has_short_event_cooldown_and_actual_task_context(self):
        engine = WageEngine(DEFAULTS)
        engine.play.start_task()
        app = SimpleNamespace(cfg=copy.deepcopy(config.DEFAULTS), wage=engine,
                              brain=SimpleNamespace(ai=AsyncMock(return_value=('这需求谁写的啊', '疑惑'))),
                              pipe=SimpleNamespace(react=Mock(), narrate_fresh=Mock()))
        player = WagePlayer(app, DEFAULTS)
        player._last_at = time.monotonic() - 2
        self.assertTrue(await player._speak({'type': 'task_start', 'title': engine.play.round['title']}))
        prompt = app.brain.ai.call_args.args[1]
        self.assertIn(engine.play.round['title'], prompt)
        app.pipe.narrate_fresh.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
