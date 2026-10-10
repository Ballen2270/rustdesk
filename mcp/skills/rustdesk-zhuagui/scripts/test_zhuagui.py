#!/usr/bin/env python3
"""Counting, retry, budget, ambiguous-target and stop-boundary regressions (offline)."""
import copy
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock

from zhuagui import (CONFIRM, MODAL, TASK, ZhuaguiWorkflow, advance, begin_action,
                    build_screens, build_workflow, make_state, map_position,
                    row_button, snap_from_anchors, targets, titlebar_runs, validate,
                    cleanup_frames, unique_text, ACTIVITY, DIALOG_TAP)

CFG = dict(device='pc1', calibrated=True, native_resolution=[1600, 600],
           activity_list_scroll=[.65, .25, .22, .32],
           windows=[dict(name='left', rect=[0, 0, 760, 570], identity='left leader'),
                    dict(name='right', rect=[800, 0, 760, 570], identity='right leader')])


def item(text, x, y, w=.08):
    return dict(text=text, x=x, y=y, w=w, h=.03, score=.9)


def obs(state, n=None, pos=None):
    return dict(state=state, task_n=n, task_total=10 if n is not None else None,
                map_position=pos)


class Policy(unittest.TestCase):
    def setUp(self):
        self.wf = build_workflow(CFG)
        self.s = make_state(2)
        self.s['initialized'] = True
        self.rng = Mock()
        self.rng.uniform.return_value = 30

    def step(self, state, t, n=None, pos=None):
        return advance(self.s, obs(state, n, pos), t, self.wf, CFG, rng=self.rng)

    def claimed(self):
        begin_action(self.s, 'claim_task', 0)
        self.step('task', 1, 1, [96, 102])
        self.step('task', 2, 1, [96, 102])
        self.s.pop('dialog_pending', None)  # dismiss already handled

    def test_two_frame_input_gate(self):
        self.assertIsNone(self.step('normal', 0))
        self.assertEqual(self.step('normal', 1), 'open_activity')

    def test_select_daily_before_scrolling_or_participating(self):
        self.step('activity_panel', 0)
        self.assertEqual(self.step('activity_panel', 1), 'select_daily')
        begin_action(self.s, 'select_daily', 1)
        self.assertEqual(self.step('activity_panel', 2), 'join_entry')

    def test_no_count_at_tenth_fight_or_bonus(self):
        self.claimed()
        for n in range(1, 11):
            self.step('battle', n*10)
            self.step('task', n*10+1, n, [n, 10])
        self.step('battle', 111)  # Optional millennium ghost.
        self.step('battle', 9999)  # No combat timeout.
        self.assertEqual(self.s['count'], 0)
        self.step('round_done', 10000)
        self.assertEqual(self.s['count'], 1)
        self.assertNotIn('paused', self.s)

    def test_same_popup_counts_once(self):
        self.claimed()
        self.step('battle', 3)
        for t in range(4, 10):
            self.step('round_done', t)
        self.assertEqual(self.s['count'], 1)

    def test_continuation_waits_thirty_seconds_and_samples_once(self):
        self.claimed()
        self.step('battle', 3)
        for t in (4, 5, 20, 33.99):
            self.assertIsNone(self.step('round_done', t))
        self.assertEqual(self.step('round_done', 34), 'continue_round')
        self.rng.uniform.assert_called_once_with(10, 40)
        self.assertEqual(self.s['continue_at'], 34)

    def test_maximum_wait_and_unknown_frame_do_not_reset_deadline(self):
        self.rng.uniform.return_value = 150
        self.claimed()
        self.step('battle', 3)
        self.step('round_done', 4)
        self.step('unknown', 20)
        self.assertIsNone(self.step('round_done', 153.99))
        self.assertEqual(self.step('round_done', 154), 'continue_round')
        self.rng.uniform.assert_called_once_with(10, 40)

    def test_windows_wait_independently_without_blocking_other_actions(self):
        self.claimed()
        self.step('battle', 3)
        self.step('round_done', 4)
        other = make_state(2)
        other['initialized'] = True
        advance(other, obs('claim_menu'), 5, self.wf, CFG, rng=self.rng)
        self.assertEqual(advance(other, obs('claim_menu'), 6, self.wf, CFG, rng=self.rng), 'claim_task')
        begin_action(other, 'claim_task', 6)
        advance(other, obs('battle'), 7, self.wf, CFG, rng=self.rng)
        self.rng.uniform.return_value = 150
        advance(other, obs('round_done'), 8, self.wf, CFG, rng=self.rng)
        self.assertEqual(self.step('round_done', 34), 'continue_round')
        self.assertIsNone(advance(other, obs('round_done'), 34, self.wf, CFG, rng=self.rng))
        self.assertEqual(other['continue_at'], 158)

    def test_new_round_gets_new_wait(self):
        self.s['target'] = 3
        self.claimed()
        self.step('battle', 3)
        self.step('round_done', 4)
        self.assertEqual(self.step('round_done', 34), 'continue_round')
        begin_action(self.s, 'continue_round', 34)
        self.step('claim_menu', 35)
        begin_action(self.s, 'claim_task', 36)
        self.assertNotIn('continue_at', self.s)
        self.step('battle', 37)
        self.rng.uniform.return_value = 90
        self.step('round_done', 38)
        self.assertEqual(self.s['continue_at'], 128)
        self.assertEqual(self.rng.uniform.call_count, 2)

    def test_second_round_requires_fresh_combat(self):
        self.claimed()
        self.step('battle', 3)
        self.step('round_done', 4)
        begin_action(self.s, 'continue_round', 5)
        self.step('claim_menu', 6)
        begin_action(self.s, 'claim_task', 7)
        self.step('task', 8, 1)
        self.step('round_done', 9)
        self.assertEqual(self.s['count'], 1)
        self.assertNotIn('paused', self.s)
        self.assertFalse(self.s['round_open'])

    def test_stops_at_target_without_continuation(self):
        self.s['target'] = 1
        self.claimed()
        self.step('battle', 3)
        self.assertIsNone(self.step('round_done', 4))
        self.assertEqual(self.s['count'], 1)
        self.assertIsNone(self.step('round_done', 5))
        self.rng.uniform.assert_not_called()

    def test_unowned_end_skips_and_continues(self):
        self.step('round_done', 1)
        self.assertEqual(self.s['count'], 0)
        self.assertNotIn('paused', self.s)
        self.assertFalse(self.s['round_open'])
        self.assertEqual(self.s['recovered'], 'skipped_unowned_round_end')

    def test_midbattle_execute_start_adopts_open_round(self):
        s = make_state(1)
        self.assertIsNone(advance(s, obs('battle'), 0, self.wf, CFG))
        self.assertNotIn('paused', s)
        self.assertTrue(s['round_open'] and s['battle_seen'] and s['initialized'])
        self.assertEqual(s['recovered'], 'adopted_open_round')

    def test_midbattle_observe_allowed_without_input_or_count(self):
        s = make_state(1)
        advance(s, obs('battle'), 0, self.wf, CFG, execute=False)
        advance(s, obs('round_done'), 1, self.wf, CFG, execute=False)
        self.assertNotIn('paused', s)
        self.assertEqual(s['count'], 0)

    def test_missing_end_recovers_and_reclaims(self):
        self.claimed()
        self.step('battle', 3)
        self.step('claim_menu', 4)
        self.assertEqual(self.s['count'], 0)
        self.assertNotIn('paused', self.s)
        self.assertEqual(self.step('claim_menu', 65), 'claim_task')
        self.assertEqual(self.s['missed_rounds'], 1)
        self.assertFalse(self.s['round_open'])

    def test_unreadable_round_tail_recovers_on_map(self):
        # Live failure 2026-10-02 20:41: task hit 10/10, six minutes of
        # unreadable screen (popup never recognized, countdown cancelled it),
        # then the map: recover by re-claiming instead of freezing.
        self.s.update(mode='double', points=360)
        self.claimed()
        self.step('battle', 3)
        for n in range(2, 11):
            self.step('task', n*5, n)
            self.step('battle', n*5+1)
        for t in range(100, 160, 10):
            self.assertIn(self.step('unknown', t), (None, 'dismiss_overlay'))
        self.assertIsNone(self.step('normal', 165))
        # grace expires: recovery and the re-claim intent land in the same poll
        self.assertEqual(self.step('normal', 230), 'open_activity')
        self.assertEqual(self.s['missed_rounds'], 1)
        self.assertEqual(self.s['estimated_spent'], 40)
        self.assertFalse(self.s['round_open'])


    def test_manually_claimed_round_is_adopted_and_counted(self):
        # Live failure 2026-10-02 21:42: the user manually re-engaged a round;
        # adopt it and count at its round-end instead of pausing.
        s = make_state(9)
        s['count'] = 8
        s['initialized'] = True
        self.assertIsNone(advance(s, obs('battle'), 1, self.wf, CFG))
        self.assertTrue(s['round_open'] and s['battle_seen'])
        self.assertEqual(s['recovered'], 'adopted_open_round')
        advance(s, obs('round_done'), 2, self.wf, CFG)
        self.assertEqual(s['count'], 9)
        self.assertNotIn('paused', s)

    def test_unknown_withholds_input(self):
        self.claimed()
        self.assertIsNone(self.step('unknown', 3))
        self.assertIsNone(self.step('unknown', 30))
        self.assertIn('warning', self.s)
        self.assertNotIn('paused', self.s)

    def test_nav_waits_for_auto_movement(self):
        self.claimed()
        self.assertIsNone(self.step('task', 5, 1, [95, 103]))
        self.assertIsNone(self.step('task', 6, 1, [90, 104]))
        self.assertEqual(self.step('task', 10, 1, [90, 104]), 'goto_ghost')

    def test_nav_retry_exactly_once_on_still_coordinates(self):
        self.claimed()
        self.assertEqual(self.step('task', 5, 1, [96, 102]), 'goto_ghost')
        begin_action(self.s, 'goto_ghost', 5)
        self.s['nav_task_n'] = 1
        self.assertIsNone(self.step('task', 6, 1, [96, 102]))
        self.assertEqual(self.step('task', 14, 1, [96, 102]), 'goto_ghost')
        begin_action(self.s, 'goto_ghost', 14)
        self.assertIsNone(self.step('task', 24, 1, [96, 102]))
        self.assertIsNone(self.step('task', 165, 1, [96, 102]))
        self.assertEqual(self.s['nav_attempts'], 2)
        self.assertIn('paused', self.s)

    def test_no_nav_retry_without_recognized_coordinates(self):
        self.claimed()
        begin_action(self.s, 'goto_ghost', 5)
        self.s['nav_task_n'] = 1
        self.assertIsNone(self.step('task', 14, 1))
        self.assertEqual(self.s['nav_attempts'], 1)
        self.assertIn('pending', self.s)

    def test_map_position_accepts_dot_and_split_coordinate_ocr(self):
        # Live run 2026-10-02: Vision read the small-font comma as '.' (8.png)
        # and split the line into two items (9.png), starving the retry gate.
        self.assertEqual(map_position([item('已时（96.97）', .07, .07)]), [96, 97])
        self.assertEqual(map_position([item('已时', .07, .07),
                                       item('（90.99）', .12, .07)]), [90, 99])
        self.assertIsNone(map_position([item('12:27', .07, .003)]))

    def test_position_read_after_click_still_allows_single_retry(self):
        begin_action(self.s, 'claim_task', 0)
        self.assertIsNone(self.step('task', 1, 1))   # coordinates never readable
        self.s.pop('dialog_pending', None)           # dismiss already handled
        self.assertIsNone(self.step('task', 2, 1))
        self.assertEqual(self.step('task', 5, 1), 'goto_ghost')
        begin_action(self.s, 'goto_ghost', 5)        # click-time frame read nothing
        self.s['nav_task_n'] = 1
        self.s['nav_position'] = None
        self.assertIsNone(self.step('task', 8, 1))            # still unreadable
        self.assertIsNone(self.step('task', 10, 1, [96, 97]))  # adopts anchor
        self.assertIsNone(self.step('task', 12, 1, [96, 97]))
        self.assertEqual(self.step('task', 14, 1, [96, 97]), 'goto_ghost')
        begin_action(self.s, 'goto_ghost', 14)
        self.assertIsNone(self.step('task', 24, 1, [96, 97]))
        self.assertIsNone(self.step('task', 165, 1, [96, 97]))
        self.assertEqual(self.s['nav_attempts'], 2)
        self.assertIn('paused', self.s)

    def test_insufficient_points_popup_cancels(self):
        # Chance popup right after claiming: no double points — click 取消.
        self.claimed()
        self.assertEqual(self.step('insufficient_popup', 6), 'dismiss_insufficient')
        begin_action(self.s, 'dismiss_insufficient', 6)
        self.assertIsNone(self.step('task', 8, 1))   # cancelled, flow resumes

    def test_adopted_anchor_never_retries_while_moving(self):
        begin_action(self.s, 'claim_task', 0)
        self.step('task', 1, 1)
        self.s.pop('dialog_pending', None)
        self.step('task', 2, 1)
        self.assertEqual(self.step('task', 5, 1), 'goto_ghost')
        begin_action(self.s, 'goto_ghost', 5)
        self.s['nav_task_n'] = 1
        self.s['nav_position'] = None
        self.assertIsNone(self.step('task', 10, 1, [96, 97]))
        self.assertIsNone(self.step('task', 14, 1, [80, 88]))   # moving: no retry
        self.assertEqual(self.s['nav_attempts'], 1)

    def test_dismiss_dialog_precedes_task_panel_click(self):
        self.claimed()
        self.s['dialog_pending'] = True   # claim confirmed with NPC dialog up
        self.assertEqual(self.step('task', 5, 1, [96, 102]), 'dismiss_dialog')
        begin_action(self.s, 'dismiss_dialog', 5)
        self.assertNotIn('dialog_pending', self.s)
        self.assertEqual(self.step('task', 6, 1, [96, 102]), 'goto_ghost')

    def test_dismiss_uses_fixed_scene_spot_not_panel(self):
        wf = build_workflow(CFG)
        node = wf.nodes['dismiss_dialog']
        bx, by, bw, bh = node.act.box
        self.assertLess(bx+bw, TASK[0])          # never inside the task panel
        self.assertGreater(by, .15)              # below the top bar
        self.assertLess(by+bh, .75)              # above the bottom bars

    def test_round_done_accepts_either_core_pair(self):
        wf = ZhuaguiWorkflow(build_screens(), [])
        self.assertEqual(wf.classify([item('捉完1轮鬼', .45, .45),
                                      item('继续捉鬼', .46, .48)]), 'round_done')
        self.assertEqual(wf.classify([item('是否继续捉鬼', .45, .45),
                                      item('确定', .56, .56)]), 'round_done')
        self.assertEqual(wf.classify([item('是否继续', .45, .45)]), 'unknown')

    def test_claim_menu_drops_variable_decoration(self):
        wf = ZhuaguiWorkflow(build_screens(), [])
        self.assertEqual(wf.classify([item('请选择要做的事', .75, .20),
                                      item('捉鬼任务', .78, .30)]), 'claim_menu')

    def test_mid_round_menu_gets_assist_grace(self):
        self.claimed()
        self.assertIsNone(self.step('claim_menu', 10))
        self.assertIsNone(self.step('claim_menu', 69))
        self.assertNotIn('paused', self.s)
        self.assertEqual(self.step('claim_menu', 71), 'claim_task')   # recovered
        self.assertEqual(self.s['missed_rounds'], 1)

    def test_window_snap_from_topbar_anchors(self):
        def hit(text, cx, cy):
            w, h = .04, .02
            return dict(text=text, score=.9, x=cx/1920-w/2, y=cy/1080-h/2, w=w, h=h)
        # right window actually sits at x=988 (w 930); config remembers 962 (w 958)
        items = [hit('指引', 1205, 31+.069*718), hit('活动', 1272, 31+.069*718),
                 hit('排行', 1323, 31+.069*718),
                 hit('指引', 224, 31+.069*718), hit('活动', 292, 31+.069*718)]
        snapped = snap_from_anchors(items, (1920, 1080), [962, 31, 958, 718],
                                    [[0, 31, 954, 718]])
        self.assertAlmostEqual(snapped[0], 988, delta=15)
        self.assertEqual(snapped[1:], [31, 958, 718])

    def test_marquee_text_never_anchors_a_window(self):
        def hit(text, cx, cy, score=.9):
            w, h = .04, .02
            return dict(text=text, score=score, x=cx/1920-w/2, y=cy/1080-h/2, w=w, h=h)
        # Real top bar at y≈80 plus the chat marquee row at y≈130 that also
        # contains 活动 (live trap seen 2026-10-02 19:45).
        good = [hit('指引', 224, 80), hit('活动', 292, 80), hit('排行', 344, 80)]
        self.assertIsNotNone(snap_from_anchors(good, (1920, 1080), [0, 31, 954, 718], []))
        marquee = [hit('活动', 473, 132, .5),
                   hit('•在青丘奇珍活动中仅用5抽就获得了灵狐栖梦礼盒', 1427, 129, .5)]
        self.assertIsNone(snap_from_anchors(marquee, (1920, 1080),
                                            [0, 31, 954, 718], [[962, 31, 958, 718]]))
        lone_real_activity = [hit('活动', 292, 80)]
        self.assertIsNone(snap_from_anchors(lone_real_activity, (1920, 1080),
                                            [0, 31, 954, 718], []))
        polluted = good + marquee
        snapped = snap_from_anchors(polluted, (1920, 1080), [0, 31, 954, 718],
                                    [[962, 31, 958, 718]])
        self.assertAlmostEqual(snapped[0], 0, delta=15)
        self.assertAlmostEqual(snapped[1], 31, delta=2)

    def test_titlebar_fallback_segments_wallpaper_gaps(self):
        from PIL import Image as PILImage
        img = PILImage.new('RGB', (820, 60), (20, 110, 180))       # blue wallpaper
        for x0, x1 in ((0, 390), (410, 820)):
            for x in range(x0, x1):
                for y in range(24):
                    img.putpixel((x, y), (250, 250, 250))          # white title bar
        self.assertEqual(titlebar_runs(img), [(0, 390, 24), (410, 820, 24)])

    def test_titlebar_fallback_scans_configured_row(self):
        # Four tiled windows: the upper row owns the screen top, the
        # configured row's title bar sits at y 250..277 above content 278.
        # The screen-top strip must not hijack the fallback.
        from PIL import Image as PILImage
        img = PILImage.new('RGB', (820, 500), (20, 110, 180))
        for x0, x1 in ((0, 390), (410, 820)):
            for x in range(x0, x1):
                for y in list(range(24)) + list(range(250, 278)):
                    img.putpixel((x, y), (250, 250, 250))
        for hint in (278, 272):  # exact and a few px low both lock onto 278
            self.assertEqual(titlebar_runs(img, hint),
                             [(0, 390, 278), (410, 820, 278)])
        self.assertEqual(titlebar_runs(img), [(0, 390, 24), (410, 820, 24)])

    def test_other_unconfirmed_inputs_never_retry(self):
        begin_action(self.s, 'claim_task', 1)
        self.step('claim_menu', 2)
        self.assertIsNone(self.step('claim_menu', 40))
        self.assertIn('paused', self.s)
        self.assertEqual(self.s['pending'], 'claim_task')

    def test_double_spending_and_two_windows_independent(self):
        self.s.update(mode='double', points=80)
        self.claimed()
        self.step('battle', 3)
        self.step('round_done', 4)
        self.step('round_done', 5)
        other = make_state(2, 'double', 80)
        self.assertEqual(self.s['estimated_spent'], 40)
        self.assertEqual(other['estimated_spent'], 0)
        self.assertEqual(other['count'], 0)


class RecognitionRules(unittest.TestCase):
    def setUp(self):
        self.wf = ZhuaguiWorkflow(build_screens(), [])

    def test_normal_map_can_use_rank_when_guide_misreads(self):
        self.assertEqual(self.wf.classify([item('活动', .28, .06, .04),
                                          item('排行', .35, .06, .04)]), 'normal')

    def test_activity_click_excludes_merged_rank_label(self):
        data = [item('活动排行', .28, .06, .12), item('活动', .28, .06, .04)]
        self.assertEqual(unique_text(data, '活动', ACTIVITY)['w'], .04)

    def test_partial_modal_blocks_background_task(self):
        data = [item('捉鬼(10/10)', .79, .21), item('是否继续捉鬼', .35, .46)]
        self.assertEqual(self.wf.classify(data), 'unknown')
        data += [item('捉完1轮鬼', .4, .46), item('确定', .57, .56)]
        self.assertEqual(self.wf.classify(data), 'round_done')

    def test_end_cancel_is_not_battle(self):
        self.assertEqual(self.wf.classify([item('2', .48, .02), item('取消(296)', .35, .56)]), 'unknown')
        self.assertEqual(self.wf.classify([item('2', .48, .02), item('取消', .94, .95, .05)]), 'battle')

    def test_battle_transition_blocks_task_clicks(self):
        data = [item('捉鬼(1/10)', .79, .21), item('2', .48, .02), item('取消', .94, .95, .05)]
        self.assertEqual(self.wf.classify(data), 'battle')

    def test_activity_button_does_not_cross_columns(self):
        data = [item('捉鬼任务', .31, .21), item('参加', .86, .24, .05),
                item('参加', .50, .24, .05), item('参加', .50, .37, .05)]
        self.assertEqual(row_button(data)[1]['x'], .50)

    def test_ambiguous_target_rejected_duplicate_ocr_merged(self):
        data = [item('捉鬼任务', .67, .21), item('捉鬼任务', .67, .21), item('参加', .86, .24, .05)]
        self.assertIsNotNone(row_button(data)[1])
        data.append(item('捉鬼任务', .31, .21))
        self.assertEqual(row_button(data), (None, None))


class Config(unittest.TestCase):
    def test_frame_cleanup_preserves_actions_and_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            keep = ['1-left-claim-before.png', '2-left-claim-after.png', 'events.jsonl', 'state.json']
            remove = ['latest-left.png', '3-left-battle.png']
            for name in keep+remove:
                (root/name).write_text('fixture')
            cleanup_frames(root)
            self.assertEqual(sorted(p.name for p in root.iterdir()), sorted(keep))

    def test_default_five_each(self):
        self.assertEqual([p[0] for p in validate(copy.deepcopy(CFG))], [5, 5])

    def test_double_floor_and_remainder(self):
        cfg = copy.deepcopy(CFG)
        cfg['windows'][0].update(mode='double', double_points=180, reward_mode_confirmed=True)
        self.assertEqual(targets(cfg)[0], (4, 'double', 180))
        cfg['windows'][0]['double_points'] = 20
        self.assertEqual(targets(cfg)[0][0], 0)

    def test_points_required_per_window(self):
        cfg = copy.deepcopy(CFG)
        cfg['mode'] = 'double'
        cfg['windows'][0].update(double_points=200, reward_mode_confirmed=True)
        with self.assertRaises(ValueError):
            targets(cfg)

    def test_double_mode_must_be_confirmed(self):
        cfg = copy.deepcopy(CFG)
        cfg['windows'][0].update(mode='double', double_points=200)
        with self.assertRaises(ValueError):
            targets(cfg)

    def test_override_cannot_overrun_budget(self):
        cfg = copy.deepcopy(CFG)
        cfg['windows'][0].update(mode='double', double_points=180, reward_mode_confirmed=True)
        with self.assertRaises(ValueError):
            targets(cfg, 5)

    def test_overlap_and_stale_calibration_rejected(self):
        cfg = copy.deepcopy(CFG)
        cfg['windows'][1]['rect'] = [500, 0, 760, 570]
        with self.assertRaises(ValueError):
            validate(cfg)
        with self.assertRaises(ValueError):
            validate({**CFG, 'calibrated': False})


if __name__ == '__main__':
    unittest.main()
