"""Offline behavioral regression tests for the mijing workflow; never
connect to a device and never run OCR (synthetic items only)."""
import random
import unittest

from mijing import MijingWorkflow, battle_hits, bookkeep, build_screens, build_workflow, validate
from _framework.humanize import ClickPlanner, ScrollPlanner
from _framework.workflow import find_row_button, find_text, level_of


def item(text, x, y, w, h, score=1.0):
    return dict(text=text, score=score, x=x, y=y, w=w, h=h)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.wf = MijingWorkflow(build_screens(), [])

    def classify(self, *items):
        return self.wf.classify(list(items))

    def test_priority_menu_over_instance(self):
        # 18关-style frame: instance info panel AND fight menu visible → menu wins
        items = [item('秘境降妖', .80, .36, .12, .04), item('第18关:挑战海底元帅', .78, .41, .18, .04),
                 item('地图', .80, .47, .05, .03), item('离开', .90, .47, .05, .03),
                 item('请选择要做的事:', .82, .55, .14, .04),
                 item('进入战斗', .82, .62, .12, .05), item('观看录像', .82, .70, .12, .05)]
        self.assertEqual(self.classify(*items), 'fight_menu')

    def test_cleared_beats_instance(self):
        items = [item('秘境降妖', .80, .36, .12, .04), item('通关', .80, .41, .08, .04),
                 item('地图', .80, .47, .05, .03), item('离开', .90, .47, .05, .03)]
        self.assertEqual(self.classify(*items), 'cleared')

    def test_instance_requires_two_anchors(self):
        only_title = [item('秘境降妖', .80, .36, .12, .04)]
        self.assertEqual(self.classify(*only_title), 'unknown')
        full = only_title+[item('地图', .80, .47, .05, .03)]
        self.assertEqual(self.classify(*full), 'in_instance')

    def test_instance_forbids_continue_word(self):
        # Vertical button OCR yields separate items; the forbid anchors on 继续.
        items = [item('秘境降妖', .80, .36, .12, .04), item('地图', .80, .47, .05, .03),
                 item('继续', .83, .78, .05, .05)]
        self.assertEqual(self.classify(*items), 'unknown')

    def test_mijing_panel(self):
        # Vertical two-line button: separate 继续 / 挑战 items.
        items = [item('继续', .83, .78, .05, .05), item('挑战', .83, .81, .05, .05)]
        self.assertEqual(self.classify(*items), 'mijing_panel')

    def test_continue_alone_is_not_panel(self):
        items = [item('继续', .83, .78, .05, .05)]
        self.assertNotEqual(self.classify(*items), 'mijing_panel')

    def test_yunleyou_menu_not_fight_menu(self):
        items = [item('请选择要做的事:', .82, .38, .14, .04),
                 item('秘境降妖', .82, .48, .12, .05), item('东海湾蜃境', .82, .57, .12, .05)]
        self.assertEqual(self.classify(*items), 'yunleyou_menu')

    def test_activity_panel(self):
        items = [item('活 动'.replace(' ', ''), .5, .14, .08, .05),
                 item('日常活动', .12, .23, .12, .06), item('参加', .87, .52, .08, .05)]
        self.assertEqual(self.classify(*items), 'activity_panel')

    def test_normal_top_bar(self):
        items = [item('23:58', .02, .01, .05, .03), item('活动', .30, .05, .06, .06),
                 item('排行', .38, .05, .06, .06)]
        self.assertEqual(self.classify(*items), 'normal')

    def test_battle_toolbar_combo(self):
        items = [item('法术', .60, .85, .05, .05), item('攻击', .70, .85, .05, .05),
                 item('道具', .80, .85, .05, .05)]
        self.assertEqual(self.classify(*items), 'battle')

    def test_battle_round_and_cancel(self):
        items = [item('2', .48, .03, .03, .05, score=.6), item('取消', .93, .85, .06, .06)]
        self.assertEqual(self.classify(*items), 'battle')

    def test_battle_toolbar_needs_three(self):
        # Two toolbar words without round+cancel is not a battle.
        items = [item('法术', .60, .85, .05, .05), item('攻击', .70, .85, .05, .05),
                 item('包裹', .95, .85, .05, .05)]
        self.assertFalse(battle_hits(items))


class BattleHitsTests(unittest.TestCase):
    def test_round_and_cancel(self):
        items = [item('12', .46, .02, .06, .08, score=.5), item('取消', .92, .84, .07, .08)]
        self.assertTrue(battle_hits(items))

    def test_toolbar_three_of_five(self):
        items = [item('自动', .62, .84, .05, .05), item('防御', .72, .84, .05, .05),
                 item('法术', .82, .84, .05, .05)]
        self.assertTrue(battle_hits(items))

    def test_negative(self):
        items = [item('方寸山', .1, .05, .1, .05), item('包裹', .95, .85, .05, .05)]
        self.assertFalse(battle_hits(items))


class HelperTests(unittest.TestCase):
    def test_level_of(self):
        self.assertEqual(level_of([item('第18关:挑战海底元帅', .78, .41, .18, .04)]), 18)
        self.assertEqual(level_of([item('第1关：挑战猛如虎', .78, .41, .18, .04)]), 1)
        self.assertIsNone(level_of([item('通关', .8, .4, .1, .05)]))
        self.assertIsNone(level_of([item('第abc关', .78, .41, .18, .04)]))

    def test_find_text_box_and_score(self):
        items = [item('继续挑战', .80, .81, .12, .06, score=.7),
                 item('挑战', .10, .10, .10, .05, score=.9)]
        got = find_text(items, '继续挑战', (.60, .55, .40, .36))
        self.assertEqual(got['text'], '继续挑战')
        self.assertIsNone(find_text(items, '继续挑战', (0, 0, .2, .2)))

    def test_find_row_button(self):
        # Game buttons sit ~.06 below their row text: center-distance band,
        # nearest wins, must stay right of the entry text.
        items = [item('秘境降妖', .67, .47, .10, .04),
                 item('参加', .87, .52, .07, .05),
                 item('参加', .51, .50, .07, .05),
                 item('参加', .87, .30, .07, .05)]
        target, btn = find_row_button(items, '秘境降妖', '参加', (.20, .17, .78, .52))
        self.assertIsNotNone(target)
        self.assertEqual(btn['x'], .87)  # right of the entry, same row band
        _, missing = find_row_button([item('秘境降妖', .67, .47, .10, .04)],
                                     '秘境降妖', '参加', (.20, .17, .78, .52))
        self.assertIsNone(missing)

    def test_row_button_must_stay_in_search_box(self):
        # A same-band button outside the list area must not be picked.
        items = [item('秘境降妖', .67, .47, .10, .04),
                 item('参加', .95, .47, .07, .05)]  # x center .985 > box right .98
        _, btn = find_row_button(items, '秘境降妖', '参加', (.20, .17, .78, .52))
        self.assertIsNone(btn)


class PlannerTests(unittest.TestCase):
    def test_deterministic_with_seed(self):
        a = ClickPlanner(random.Random(7))
        b = ClickPlanner(random.Random(7))
        box = (.6, .4, .2, .1)
        self.assertEqual(a.point('t', box, (1000, 800)), b.point('t', box, (1000, 800)))
        self.assertEqual(a.delay(True), b.delay(True))

    def test_point_inside_box_and_no_recent_repeat(self):
        # Contract: inside the (shrunk) box, and never within min L1 distance
        # of the target's last `history` landing points.
        p = ClickPlanner(random.Random(1), history=50, min_repeat_dist=2.0)
        box = (.6, .4, .2, .1)
        recent = []
        for _ in range(200):
            x, y = p.point('t', box, (1000, 800))
            self.assertTrue(.6*1000 <= x <= .8*1000 and .4*800 <= y <= .5*800)
            for a, b in recent[-50:]:
                self.assertGreater(abs(x-a)+abs(y-b), 0)
            recent.append((x, y))

    def test_histories_are_per_target(self):
        p = ClickPlanner(random.Random(2))
        box = (.6, .4, .2, .1)
        first = p.point('t', box, (1000, 800))
        second = p.point('t', box, (1000, 800))
        self.assertNotEqual(first, second)  # same target: history enforces distance
        other = p.point('other', box, (1000, 800))
        self.assertTrue(.6*1000 <= other[0] <= .8*1000)  # independent history, still in box

    def test_delay_ranges(self):
        p = ClickPlanner(random.Random(3), delay_range=(.1, .2), read_range=(.3, .4),
                         think_prob=0)
        self.assertTrue(.1 <= p.delay() <= .2)
        self.assertTrue(.4 <= p.delay(fresh_screen=True) <= .6)

    def test_scroll_segments(self):
        s = ScrollPlanner(random.Random(4))
        segs = s.segment((.6, .5), 1, (1000, 800))
        self.assertTrue(2 <= len(segs) <= 5)
        for x, y, dy, pause in segs:
            self.assertTrue(0 <= x <= 1000 and 0 <= y <= 800)
            self.assertGreaterEqual(dy, 1)
            self.assertLessEqual(dy, 3)
            self.assertTrue(.08 <= pause <= .25)


class GuardTests(unittest.TestCase):
    def test_advance_guard_needs_idle_delay(self):
        cfg = dict(device='pc1', calibrated=True, native_resolution=[1278, 956],
                   windows=[dict(name='w', rect=[0, 0, 900, 700])],
                   activity_button=[.2, 0, .2, .1], activity_list_scroll=[.4, .2, .4, .4],
                   advance_delay=5)
        wf = build_workflow(cfg)
        node = wf.nodes['advance_level']
        s = dict(inst_since=None)
        self.assertFalse(node.guard(dict(s=s, now=100.0, advance_delay=5)))
        s['inst_since'] = 97.0
        self.assertFalse(node.guard(dict(s=s, now=100.0, advance_delay=5)))
        self.assertTrue(node.guard(dict(s=s, now=102.5, advance_delay=5)))

    def test_nodes_for_source(self):
        cfg = dict(device='pc1', calibrated=True, native_resolution=[1278, 956],
                   windows=[dict(name='w', rect=[0, 0, 900, 700])],
                   activity_button=[.2, 0, .2, .1], activity_list_scroll=[.4, .2, .4, .4])
        wf = build_workflow(cfg)
        self.assertEqual([n.id for n in wf.nodes_for('normal')], ['open_activity'])
        self.assertEqual([n.id for n in wf.nodes_for('fight_menu')], ['enter_fight'])
        self.assertEqual(wf.nodes_for('battle'), [])


class BookkeepTests(unittest.TestCase):
    """Pause-don't-guess anomalies from the Codex review: failed level
    replays and unearned cleared screens must reconcile, not retry."""

    def fresh(self):
        return dict(count=0, active=False, battle_seen=False, inst_since=None,
                    inst_level=None, seen=None, pre_battle_level=None)

    def feed(self, s, seq, cfg=None, t0=100.0):
        cfg = cfg or {}
        obs = {'level': None}
        for i, (state, level) in enumerate(seq):
            obs = dict(state=state, level=level)
            bookkeep(s, obs, t0+i, cfg)
            s['seen'] = state
        return obs

    def test_failed_level_replay_pauses(self):
        s = self.fresh()
        self.feed(s, [('in_instance', 5), ('battle', None), ('in_instance', 5)])
        self.assertIn('did not advance', s.get('paused', ''))

    def test_advanced_level_continues(self):
        s = self.fresh()
        self.feed(s, [('in_instance', 5), ('battle', None), ('in_instance', 6)])
        self.assertIsNone(s.get('paused'))
        self.assertIsNone(s['pre_battle_level'])

    def test_level_none_after_battle_defers(self):
        s = self.fresh()
        self.feed(s, [('in_instance', 5), ('battle', None), ('in_instance', None)])
        self.assertIsNone(s.get('paused'))
        self.assertEqual(s['pre_battle_level'], 5)

    def test_boss_chain_keeps_pre_level_until_instance(self):
        # fight_menu must not clear the pending pre-battle level.
        s = self.fresh()
        self.feed(s, [('in_instance', 17), ('battle', None), ('fight_menu', 17),
                      ('battle', None), ('in_instance', 17)])
        self.assertIn('did not advance', s.get('paused', ''))

    def test_boss_chain_advanced_level_not_paused(self):
        s = self.fresh()
        self.feed(s, [('in_instance', 17), ('battle', None), ('fight_menu', 18),
                      ('battle', None), ('in_instance', 18)])
        self.assertIsNone(s.get('paused'))

    def test_cleared_without_battle_pauses(self):
        s = self.fresh()
        self.feed(s, [('normal', None), ('cleared', None)])
        self.assertIn('cleared without', s.get('paused', ''))

    def test_cleared_after_battle_ok(self):
        s = self.fresh()
        self.feed(s, [('battle', None), ('cleared', None)])
        self.assertIsNone(s.get('paused'))
        self.assertTrue(s['battle_seen'])

    def test_stall_after_battle_pauses(self):
        s = self.fresh()
        self.feed(s, [('in_instance', 5), ('battle', None)], t0=0.0)
        self.feed(s, [('in_instance', 5)], cfg={'advance_stall': 30}, t0=0.0)
        # same level, but inst_since resets on re-entry; force the elapsed time
        s['inst_since'] = -1000
        bookkeep(s, dict(state='in_instance', level=5), 0.0, {'advance_stall': 30})
        self.assertIn('no progress', s.get('paused', ''))


class ValidateTests(unittest.TestCase):
    def base(self):
        return dict(device='pc1', calibrated=True, native_resolution=[1278, 956],
                    windows=[dict(name='w1', rect=[0, 0, 900, 700])],
                    activity_button=[.2, 0, .2, .1], activity_list_scroll=[.4, .2, .4, .4])

    def test_ok(self):
        validate(self.base(), rounds=1)

    def test_more_than_eight_accounts(self):
        cfg = self.base()
        cfg['windows'] = [dict(name='w'+str(i), device='pc'+str(i), rect=[0, 0, 900, 700])
                          for i in range(12)]
        validate(cfg, 1)

    def test_empty_or_duplicate_account_names_rejected(self):
        cfg = self.base()
        for windows in ([], [dict(name='same', device='pc'+str(i), rect=[0, 0, 900, 700])
                            for i in range(2)]):
            cfg['windows'] = windows
            with self.assertRaisesRegex(ValueError, 'at least one account window'):
                validate(cfg, 1)

    def test_uncalibrated(self):
        cfg = self.base()
        cfg['calibrated'] = False
        with self.assertRaises(ValueError):
            validate(cfg, 1)

    def test_overlap_same_device(self):
        cfg = self.base()
        cfg['windows'] = [dict(name='a', rect=[0, 0, 900, 700]),
                          dict(name='b', rect=[100, 100, 900, 700])]
        with self.assertRaises(ValueError):
            validate(cfg, 1)

    def test_bad_rounds_bounds(self):
        cfg = self.base()
        cfg['initial_counts'] = [2]
        with self.assertRaises(ValueError):
            validate(cfg, 1)

    def test_bad_stall(self):
        cfg = self.base()
        cfg['advance_stall'] = 5
        with self.assertRaises(ValueError):
            validate(cfg, 1)

    def test_rounds_out_of_range(self):
        cfg = self.base()
        cfg['rounds'] = 0
        with self.assertRaises(ValueError):
            validate(cfg, 0)  # resolved from config when --rounds is absent
        with self.assertRaises(ValueError):
            validate(cfg, 11)

    def test_box_extent_beyond_window(self):
        cfg = self.base()
        cfg['activity_button'] = [.9, 0, .3, .1]  # x+w = 1.2 > 1
        with self.assertRaises(ValueError):
            validate(cfg, 1)
        cfg = self.base()
        cfg['activity_list_scroll'] = [0, .95, .3, .2]  # y+h > 1
        with self.assertRaises(ValueError):
            validate(cfg, 1)


if __name__ == '__main__':
    unittest.main()
