#!/usr/bin/env python3
"""Planning, cycle-driving, retry and stop-boundary regressions (offline)."""
import unittest
from unittest.mock import Mock

from fuben import (DBUTTONS, DLG_OPT, FubenWorkflow, advance, begin_action,
                   build_screens, build_workflow, default_knight, dialog_option,
                   enter_button, make_state, next_target, open_run, ready_buttons,
                   reject_bands, snap_from_anchors, task_row, titlebar_runs,
                   validate, wildcard_row)

CFG = dict(device='pc1', calibrated=True, native_resolution=[1600, 600],
           activity_list_scroll=[.65, .25, .22, .32],
           windows=[dict(name='left', rect=[0, 0, 760, 570], identity='left leader'),
                    dict(name='right', rect=[800, 0, 760, 570], identity='right leader')])


def item(text, x, y, w=.08, h=.03, score=.9):
    return dict(text=text, x=x, y=y, w=w, h=h, score=score)


def obs(state, pos=None):
    return dict(state=state, map_position=pos)


class Policy(unittest.TestCase):
    def test_pending_battle_confirmation_keeps_active_for_unknown(self):
        self.s.update(run_open=True, entering='knight')
        begin_action(self.s, 'dialog_confirm', 0)
        self.step('battle', 1)
        self.assertNotIn('pending', self.s)
        self.assertTrue(self.s['active'] and self.s['battle_seen'])
        for t in (2, 35, 1000):
            self.assertIsNone(self.step('unknown', t))
        self.assertNotIn('dismiss_tries', self.s)
        self.assertNotIn('paused', self.s)
        self.assertIn('warning', self.s)

    def test_battle_unknown_does_not_right_click_but_map_overlay_can(self):
        self.s.update(run_open=True, entering='knight')
        self.step('battle', 0)
        self.step('unknown', 1)
        self.assertIsNone(self.step('unknown', 33))
        self.step('dungeon_map', 34)
        self.step('unknown', 35)
        self.assertEqual(self.step('unknown', 66), 'dismiss_overlay')

    def test_activity_timeout_does_not_right_click_active_battle(self):
        begin_action(self.s, 'open_activity', 0)
        self.assertIsNone(self.step('battle', 21))
        self.assertTrue(self.s['active'])

    def setUp(self):
        self.wf = build_workflow(CFG)
        self.s = make_state(2, 2)
        self.s['initialized'] = True

    def step(self, state, t, items=(), pos=None):
        return advance(self.s, obs(state, pos), list(items), t, self.wf, CFG)

    # ---- planning ----

    def test_monday_wants_three_knight_runs(self):
        import fuben
        real = fuben.datetime.date

        class Mon(real):
            @classmethod
            def today(cls):
                return cls(2026, 9, 28)   # Monday

        class Tue(real):
            @classmethod
            def today(cls):
                return cls(2026, 9, 29)   # Tuesday
        fuben.datetime.date = Mon
        try:
            self.assertEqual(default_knight(), 3)
            fuben.datetime.date = Tue
            self.assertEqual(default_knight(), 2)
        finally:
            fuben.datetime.date = real

    def test_knight_before_normal_and_target_zero_stops(self):
        self.assertEqual(next_target(self.s), 'knight')
        self.s['knight'] = 2
        self.assertEqual(next_target(self.s), 'normal')
        self.s['normal'] = 2
        self.assertIsNone(next_target(self.s))

    # ---- two-frame input gate + between-run path ----

    def test_two_frame_input_gate(self):
        self.assertIsNone(self.step('normal', 0))
        self.assertEqual(self.step('normal', 1), 'open_activity')

    def test_between_run_walks_panel_path(self):
        self.step('normal', 1)
        self.step('activity_panel', 2)                      # streak gate
        self.assertEqual(self.step('activity_panel', 3), 'select_daily')
        begin_action(self.s, 'select_daily', 3)
        self.step('activity_panel', 4)
        row = [item('蹈海去-普通四', .38, .49, w=.14)]
        self.assertEqual(self.step('activity_panel', 5, row), 'join_dungeon')
        begin_action(self.s, 'join_dungeon', 5)
        self.step('baixiao_menu', 6)
        self.assertEqual(self.step('baixiao_menu', 7), 'open_dungeon_menu')
        begin_action(self.s, 'open_dungeon_menu', 7)
        self.step('dungeon_panel', 8)
        self.assertEqual(self.step('dungeon_panel', 9), 'pick_knight_tab')
        begin_action(self.s, 'pick_knight_tab', 9)
        self.assertIsNone(self.step('dungeon_panel', 10))  # pending confirms
        self.assertEqual(self.step('dungeon_panel', 11), 'enter_dungeon')
        begin_action(self.s, 'enter_dungeon', 11)
        self.assertEqual(self.s['pending'], 'enter_dungeon')
        self.step('knight_wait', 12)                      # confirm opens the run
        self.assertTrue(self.s['run_open'])

    def test_panel_without_dungeon_rows_retabs_before_hunting(self):
        # run4 right: the panel read a full 80 items with zero -普通 rows
        # (wrong tab / swallowed tab click) — re-select 日常活动 twice (also
        # resets the list scroll), then hand over to the join search.
        self.s['daily_selected'] = True
        empty = [item('星宿每周0/15', .66, .62), item('参加', .78, .38)]
        self.assertIsNone(self.step('activity_panel', 3, empty))      # streak gate
        self.assertEqual(self.step('activity_panel', 4, empty), 'select_daily')
        self.assertEqual(self.s['retab_tries'], 1)
        begin_action(self.s, 'select_daily', 4)
        self.step('activity_panel', 5, empty)                         # pending confirms
        self.assertEqual(self.step('activity_panel', 6, empty), 'select_daily')
        self.assertEqual(self.s['retab_tries'], 2)
        begin_action(self.s, 'select_daily', 6)
        self.step('activity_panel', 7, empty)                         # pending confirms
        self.assertEqual(self.step('activity_panel', 8, empty), 'join_dungeon')
        rows = empty+[item('蹈海去-普通四', .38, .49, w=.14), item('参加', .51, .52, w=.05)]
        begin_action(self.s, 'join_dungeon', 8)
        self.step('baixiao_menu', 9)
        self.assertNotIn('retab_tries', self.s)

    def test_swallowed_enter_click_is_reclicked(self):
        # run12: the enter click was swallowed (cursor fought by hand) and
        # the panel sat unchanged; a fresh re-click must line up well before
        # the 60s timeout, bounded by the requeue budget.
        self.s['tab_pending'] = 'knight'
        begin_action(self.s, 'enter_dungeon', 10)          # attempts=1
        self.assertIsNone(self.step('dungeon_panel', 21))  # 11s: keep waiting
        self.assertEqual(self.step('dungeon_panel', 23), 'enter_dungeon')
        self.assertNotIn('pending', self.s)
        begin_action(self.s, 'enter_dungeon', 23)          # attempts=2
        self.s['enter_attempts'] = 3                       # budget spent
        begin_action(self.s, 'enter_dungeon', 40)
        self.assertIsNone(self.step('dungeon_panel', 54))  # no reclick past budget
        self.assertEqual(self.s['pending'], 'enter_dungeon')

    def test_swallowed_join_click_is_reclicked(self):
        # run12 right: three swallowed 参加 clicks each burned the full 180s
        # timeout and exhausted the budget; a 12s re-click recovers in-loop.
        begin_action(self.s, 'join_dungeon', 10)          # attempts=1
        self.assertIsNone(self.step('activity_panel', 21))  # 11s: keep waiting
        self.assertEqual(self.step('activity_panel', 23), 'join_dungeon')
        self.assertNotIn('pending', self.s)

    def test_dismiss_confirms_on_battle(self):
        # run12 left: a battle-transition unknown fired a dismiss; the settled
        # battle screen must confirm it instead of pausing the window.
        begin_action(self.s, 'dismiss_overlay', 10)
        self.assertIsNone(self.step('battle', 12))
        self.assertNotIn('pending', self.s)
        self.assertNotIn('paused', self.s)

    def test_normal_tab_after_knight_quota(self):
        self.s['knight'] = 2
        self.step('dungeon_panel', 1)
        self.assertEqual(self.step('dungeon_panel', 2), 'pick_normal_tab')

    # ---- knight ready screen: wait, bounce, retry ----

    def test_ready_screen_only_ever_waits(self):
        self.claimed_run()
        for t in range(10, 200, 10):
            self.assertIsNone(self.step('knight_wait', t))

    def test_ready_bounce_reenters_after_delay(self):
        self.claimed_run()
        self.step('knight_wait', 10)
        self.assertIsNone(self.step('dungeon_panel', 20))   # streak gate
        self.assertEqual(self.step('dungeon_panel', 25), 'enter_dungeon')

    def test_enter_timeout_later_pauses(self):
        self.claimed_run()
        self.s['enter_attempts'] = 40   # max_enter_retries reached
        self.step('dungeon_panel', 20)
        self.assertIn('enter_attempts', self.s)

    # ---- in-run cycle driving ----

    def claimed_run(self):
        self.s['entering'] = 'knight'
        open_run(self.s, 0)
        self.s.update(seen='dungeon_map', streak=2)

    def test_skip_button_wins_over_task_row(self):
        self.claimed_run()
        items = [item('跳过剧情动画', .84, .34), item('影之逝', .82, .26)]
        self.assertEqual(self.step('dungeon_map', 5, items), 'skip_story')

    def test_task_row_clicks_only_when_settled(self):
        self.claimed_run()
        items = [item('影之逝', .82, .26)]
        self.assertIsNone(self.step('dungeon_map', 5, items, pos=[70, 15]))
        self.assertIsNone(self.step('dungeon_map', 6, items, pos=[60, 20]))  # moving
        self.assertEqual(self.step('dungeon_map', 9, items, pos=[60, 20]), 'advance_task')

    def test_dialog_option_gate(self):
        # The prompt signature alone opens the gate — the option text rotates
        # with the story (住手！/不行！/…) and is clicked geometrically, so a
        # missing/typo'd option read must not block the click.
        self.claimed_run()
        self.assertIsNone(self.step('npc_dialog', 5))
        prompt = [item('请选择要做的事：', .78, .61, w=.17)]
        self.assertEqual(self.step('npc_dialog', 6, prompt), 'dialog_confirm')
        self.assertEqual(self.step('npc_dialog', 7, prompt+[item('得罪了！', .80, .71)]),
                         'dialog_confirm')

    def test_cutscene_after_dialog_extends_instead_of_pausing(self):
        # Live 2026-10-05: the dialog click landed but the knight story
        # cutscene read unknown past the 60s timeout — a transition in
        # flight must keep waiting, not pause the window.
        self.claimed_run()
        prompt = [item('请选择要做的事：', .78, .61, w=.17), item('得罪了！', .80, .71)]
        self.step('npc_dialog', 5, prompt)
        begin_action(self.s, 'dialog_confirm', 5)
        self.assertIsNone(self.step('unknown', 70))    # timeout, mid-cutscene
        self.assertNotIn('paused', self.s)
        self.assertIsNone(self.step('unknown', 200))   # re-armed clock
        self.assertNotIn('paused', self.s)
        self.assertIsNone(self.step('battle', 240))    # cutscene lands: confirm
        self.assertNotIn('pending', self.s)

    def test_swallowed_dialog_click_still_pauses(self):
        self.claimed_run()
        prompt = [item('请选择要做的事：', .78, .61, w=.17), item('得罪了！', .80, .71)]
        self.step('npc_dialog', 5, prompt)
        self.step('npc_dialog', 6, prompt)
        begin_action(self.s, 'dialog_confirm', 6)
        self.assertIsNone(self.step('npc_dialog', 80))  # stuck on source
        self.assertIn('paused', self.s)

    def test_reward_then_done_then_close(self):
        self.claimed_run()
        self.step('battle', 5)
        self.assertIsNone(self.step('reward', 6))            # streak gate
        self.assertEqual(self.step('reward', 7), 'close_reward')
        begin_action(self.s, 'close_reward', 7)
        self.assertIsNone(self.step('dungeon_done', 8))      # streak gate
        self.assertEqual(self.step('dungeon_done', 9), 'finish_dungeon')
        self.assertEqual(self.s['knight'], 1)     # counted at the gray box
        self.assertFalse(self.s['run_open'])
        self.assertEqual(next_target(self.s), 'knight')

    def test_done_click_finishes_without_double_count(self):
        self.claimed_run()
        self.step('dungeon_done', 6)
        self.assertEqual(self.step('dungeon_done', 7), 'finish_dungeon')
        self.assertEqual(self.s['knight'], 1)
        begin_action(self.s, 'finish_dungeon', 7)
        self.assertIsNone(self.step('dungeon_done', 8))   # run already closed
        self.assertEqual(self.s['knight'], 1)
        self.assertIsNone(self.step('normal', 20))        # pending confirms
        self.assertNotIn('paused', self.s)

    def test_normal_run_counts_normal_slot(self):
        self.s['knight'] = 2
        self.claimed_run()
        self.s['entering'] = 'normal'
        self.step('dungeon_done', 6)
        self.step('dungeon_done', 7)
        self.assertEqual(self.s['normal'], 1)

    def test_second_run_still_counts_after_first_finish(self):
        # Live mock failure: `finishing` survived the first finish click and
        # suppressed the second run's count, freezing the runner in-run.
        self.claimed_run()
        self.step('dungeon_done', 6)
        self.assertEqual(self.step('dungeon_done', 7), 'finish_dungeon')
        begin_action(self.s, 'finish_dungeon', 7)          # sets finishing
        self.step('normal', 8)                             # confirm + city
        open_run(self.s, 9)                                # second dungeon entered
        self.s['entering'] = 'knight'
        self.s.update(seen='dungeon_done', streak=2)
        self.assertEqual(self.step('dungeon_done', 10), 'finish_dungeon')
        self.assertEqual(self.s['knight'], 2)              # counted again
        self.assertFalse(self.s['run_open'])

    def test_sustained_city_closes_a_run_without_the_gray_box(self):
        self.claimed_run()
        self.assertIsNone(self.step('normal', 5))          # streak gate
        self.assertIsNone(self.step('normal', 6))          # city clock starts
        self.assertIsNone(self.step('normal', 70))         # grace expires
        self.assertEqual(self.s['recovered'], 'missed_dungeon_end')
        self.assertEqual(self.s['knight'], 1)
        self.assertFalse(self.s['run_open'])
        self.assertEqual(self.step('normal', 71), 'open_activity')

    # ---- unknown / adoption ----

    def test_unknown_withholds_input(self):
        self.claimed_run()
        self.assertIsNone(self.step('unknown', 3))
        self.assertIsNone(self.step('unknown', 30))
        self.assertIn('warning', self.s)
        self.assertNotIn('paused', self.s)

    def test_stray_overlay_gets_right_click_recovery(self):
        # Live failure 2026-10-03: a leaderboard opened by a stray click sat
        # on the map forever; right-click closes most game overlays.
        self.claimed_run()
        self.assertIsNone(self.step('unknown', 5))
        self.assertIsNone(self.step('unknown', 20))
        self.assertEqual(self.step('unknown', 40), 'dismiss_overlay')
        self.assertEqual(self.s['dismiss_tries'], 1)
        self.assertIsNone(self.step('unknown', 41))            # grace re-armed
        self.assertEqual(self.step('unknown', 80), 'dismiss_overlay')
        self.step('dungeon_map', 85, [item('影之逝', .82, .26)])
        self.assertNotIn('dismiss_tries', self.s)              # reset on known

    def test_unclosable_overlay_eventually_pauses(self):
        self.claimed_run()
        t = 5
        for _ in range(5):
            t += 1
            self.assertIsNone(self.step('unknown', t))       # clock re-arms
            t += 35
            self.assertEqual(self.step('unknown', t), 'dismiss_overlay')
        t += 1
        self.step('unknown', t)
        t += 35
        self.assertIsNone(self.step('unknown', t))
        self.assertIn('paused', self.s)

    def test_midrun_startup_is_adopted(self):
        s = make_state(2, 2)
        advance(s, obs('dungeon_map', [5, 5]), [item('影之逝', .82, .26)], 0, self.wf, CFG)
        self.assertTrue(s['run_open'])
        self.assertNotIn('paused', s)

    def test_between_run_autoentry_is_adopted(self):
        # Live 2026-10-05 (right window): an earlier queue auto-entered the
        # dungeon while the runner was re-navigating the panel path; the
        # between-run branch must adopt the open dungeon instead of ignoring
        # every in-dungeon frame forever.
        s = make_state(2, 2)
        s['initialized'] = True
        self.assertIsNone(advance(s, obs('dungeon_map', [5, 5]),
                                  [item('影之逝', .82, .26)], 0, self.wf, CFG))
        self.assertFalse(s['run_open'])          # streak 1: no adoption yet
        advance(s, obs('dungeon_map', [5, 5]), [item('影之逝', .82, .26)], 1, self.wf, CFG)
        self.assertTrue(s['run_open'])
        self.assertEqual(s['recovered'], 'adopted_open_dungeon')
        self.assertEqual(s['entering'], 'knight')

    def test_between_run_battle_autoentry_adopted(self):
        s = make_state(2, 2)
        s['initialized'] = True
        advance(s, obs('battle'), [], 0, self.wf, CFG)
        advance(s, obs('battle'), [], 1, self.wf, CFG)
        self.assertTrue(s['run_open'])
        self.assertTrue(s['battle_seen'])

    # ---- helpers ----

    def test_wildcard_row_binds_same_lane_join(self):
        items = [item('绿烟如梦-普通四', .38, .49, w=.14),
                 item('参加', .51, .52, w=.05),
                 item('蹈海去-普通四', .72, .49, w=.14),
                 item('已完成', .51, .62, w=.05)]
        entry, btn = wildcard_row(items)
        self.assertIsNotNone(btn)
        self.assertIn('绿烟如梦', entry['text'])

    def test_done_wildcard_row_is_skipped(self):
        items = [item('绿烟如梦-普通四', .38, .49, w=.14),
                 item('已完成', .51, .52, w=.05)]
        self.assertIsNone(wildcard_row(items)[1])

    def test_enter_button_takes_leftmost(self):
        items = [item('进入', .50, .78, w=.06), item('进入', .25, .78, w=.06),
                 item('已通关', .75, .78, w=.08)]
        self.assertEqual(enter_button(items)['x'], .25)

    def test_task_row_ignores_panel_tabs(self):
        items = [item('任务', .84, .17), item('影之逝', .82, .26)]
        self.assertIn('影之逝', task_row(items)['text'])

    def test_dialog_option_ignores_prompt_line(self):
        items = [item('请选择要做的事：', .75, .62, w=.16), item('得罪了！', .80, .71, w=.10)]
        self.assertIn('得罪了', dialog_option(items)['text'])

    def test_ready_screen_signature(self):
        wf = FubenWorkflow(build_screens(), [])
        self.assertEqual(wf.classify([item('正在等待其他玩家准备.', .48, .83, w=.20)]), 'knight_wait')
        self.assertEqual(wf.classify([item('操作倒计时：20秒', .48, .76, w=.16),
                                      item('未准备', .3, .61)]), 'knight_wait')

    def test_screens_prioritize_overlays_over_map(self):
        wf = FubenWorkflow(build_screens(), [])
        self.assertEqual(wf.classify([item('点击屏幕继续游戏', .50, .78, w=.16)]), 'reward')
        self.assertEqual(wf.classify([item('副本已完成', .82, .27, w=.10)]), 'dungeon_done')
        self.assertEqual(wf.classify([item('侠士副本', .36, .23), item('普通副本', .21, .23)]),
                         'dungeon_panel')

    def test_dungeon_map_needs_timechar_not_city_topbar(self):
        wf = FubenWorkflow(build_screens(), [])
        self.assertEqual(wf.classify([item('辰时（70,15）', .13, .08, w=.12)]), 'dungeon_map')
        self.assertEqual(wf.classify([item('活动', .31, .08), item('指引', .24, .08)]), 'normal')

    # ---- validate ----

    def test_validate_plans_and_knight_bounds(self):
        # knight_runs pinned: without it the expectation is weekday-dependent
        # (default_knight() is 3 on Mondays) and the test fails every Monday.
        cfg = dict(CFG, knight_runs=2)
        plans = validate(cfg)
        self.assertEqual(plans, [(2, 2), (2, 2)])
        bad = dict(CFG, knight_runs=10)
        with self.assertRaises(ValueError):
            validate(bad)

    def test_overlap_rejected(self):
        bad = dict(CFG, windows=[
            dict(name='a', rect=[0, 0, 760, 570], identity='x'),
            dict(name='b', rect=[100, 0, 760, 570], identity='y')])
        with self.assertRaises(ValueError):
            validate(bad)

    def test_marquee_never_anchors_a_window(self):
        def hit(text, cx, cy, score=.9):
            w, h = .04, .02
            return dict(text=text, score=score, x=cx/1920-w/2, y=cy/1080-h/2, w=w, h=h)
        good = [hit('指引', 224, 80), hit('活动', 292, 80), hit('排行', 344, 80)]
        self.assertIsNotNone(snap_from_anchors(good, (1920, 1080), [0, 31, 954, 718], []))
        marquee = [hit('•在青丘奇珍活动中仅用5抽就获得了灵狐栖梦礼盒', 473, 132, .5)]
        self.assertIsNone(snap_from_anchors(marquee, (1920, 1080),
                                            [0, 31, 954, 718], [[962, 31, 958, 718]]))

    def test_titlebar_fallback_segments_wallpaper_gaps(self):
        from PIL import Image as PILImage
        img = PILImage.new('RGB', (820, 60), (20, 110, 180))
        for x0, x1 in ((0, 390), (410, 820)):
            for x in range(x0, x1):
                for y in range(24):
                    img.putpixel((x, y), (250, 250, 250))
        self.assertEqual(titlebar_runs(img), [(0, 390, 24), (410, 820, 24)])

    def test_titlebar_fallback_scans_configured_row(self):
        # Four tiled windows: the upper row owns the screen top, the
        # configured row's title bar sits at y 250..277 above content 278.
        # The screen-top strip must not hijack the fallback (run11 binding).
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


class MemberReady(unittest.TestCase):
    """The member sweep's popup detection: a 准? read paired with a same-row
    拒绝 on its left is the ready button; everything else containing 准
    (the popup's own 未准备 member rows) must never pair."""

    def popup_items(self, bx=.44, by=.55, rx=.30):
        # bx/rx: 准备/拒绝 centers; the member-status row sits above.
        return [item('准备', bx-.028, by, w=.056, h=.024),
                item('拒绝', rx-.028, by, w=.056, h=.024),
                item('已准备', .07, .40, w=.06),
                item('未准备', .16, .40, w=.06),
                item('宋准盆', .25, .40, w=.06)]

    def test_button_pairs_with_reject(self):
        buttons = ready_buttons(self.popup_items())
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]['text'], '准备')

    def test_member_rows_never_pair(self):
        items = [item('已准备', .07, .40), item('未准备', .16, .40),
                 item('宋准盆', .25, .40), item('标准配置', .5, .2)]
        self.assertEqual(ready_buttons(items), [])

    def test_button_must_sit_right_of_reject(self):
        # A misread landing left of 拒绝 is rejected — clicking the other
        # button would decline the invite.
        items = [item('准备', .30-.028, .55, w=.056, h=.024),
                 item('拒绝', .44-.028, .55, w=.056, h=.024)]
        self.assertEqual(ready_buttons(items), [])

    def test_misread_glyph_still_pairs(self):
        # Measured at 615px window width: the button OCRs as 准留/淮备 while
        # 拒绝 keeps reading clean.
        for misread in ('准留', '准盆', '淮备'):
            items = [item(misread, .44-.028, .55, w=.056, h=.024),
                     item('拒绝', .30-.028, .55, w=.056, h=.024, score=.5)]
            self.assertEqual(len(ready_buttons(items)), 1, misread)

    def test_reject_in_another_window_never_pairs(self):
        # Two windows side by side: the left window's 准? must not reach the
        # right window's 拒绝 (gap beyond .45 of the frame).
        items = [item('准备', .20, .55, w=.056, h=.024),
                 item('拒绝', .80, .55, w=.056, h=.024)]
        self.assertEqual(ready_buttons(items), [])

    def test_two_popups_return_two_buttons_left_first(self):
        items = [item('准备', .74-.028, .30, w=.056, h=.024),
                 item('拒绝', .60-.028, .30, w=.056, h=.024),
                 item('准备', .40-.028, .75, w=.056, h=.024),
                 item('拒绝', .26-.028, .75, w=.056, h=.024)]
        buttons = ready_buttons(items)
        self.assertEqual([b['x'] for b in buttons], sorted(b['x'] for b in buttons))
        self.assertEqual(len(buttons), 2)

    def test_duplicate_reads_collapse_to_one(self):
        base = self.popup_items()
        dup = dict(base[0])
        dup = dict(dup, x=dup['x']+.005)
        self.assertEqual(len(ready_buttons(base+[dup])), 1)

    def test_reject_band_wraps_the_reject_row(self):
        bands = reject_bands([item('拒绝', .30, .55, w=.056, h=.024)])
        self.assertEqual(len(bands), 1)
        x, y, w, h = bands[0]
        self.assertLessEqual(x, .30)
        self.assertGreaterEqual(x+w, .30+.056+.20)   # reaches the button side
        self.assertLess(y, .55+.012)
        self.assertGreater(y+h, .55)

    def test_reject_band_ignores_non_rejects(self):
        self.assertEqual(reject_bands([item('准备', .44, .55)]), [])


class MemberConfig(unittest.TestCase):
    base = dict(CFG, native_resolution=[1920, 1080],
                windows=[dict(name='left', rect=[0, 31, 954, 718], identity='l'),
                         dict(name='right', rect=[962, 31, 958, 718], identity='r')])

    def test_members_default_empty_passes(self):
        cfg = dict(self.base)
        validate(cfg)
        self.assertEqual(cfg['members'], [])
        self.assertEqual(cfg['member_resweep'], [1.5, 3.0])

    def test_member_devices_accepted(self):
        cfg = dict(self.base, members=['pc1d1', 'pc2', 'pc3'])
        validate(cfg)
        self.assertEqual(cfg['member_resweep'], [1.5, 3.0])

    def test_member_on_leader_device_allowed(self):
        # Single-connection layout (方案 B): members share the leader device;
        # the full-frame popup sweep needs no rects for them.
        cfg = dict(self.base, members=['pc1', 'pc2', 'pc3'])
        validate(cfg)
        self.assertEqual(cfg['members'], ['pc1', 'pc2', 'pc3'])

    def test_leader_wait_screen_never_pairs(self):
        # The leader's own wait screen rides the same frame as the member
        # popups in the single-connection layout: its 未准备 rows and 取消
        # must never become a ready button (no 拒绝, no pair).
        items = [item('正在等待其他玩家准备..', .48, .83, w=.20),
                 item('操作倒计时：12秒', .46, .78, w=.16),
                 item('已准备', .22, .60, w=.06), item('未准备', .36, .60, w=.06),
                 item('取消', .88, .84, w=.06)]
        self.assertEqual(ready_buttons(items), [])

    def test_duplicate_members_rejected(self):
        cfg = dict(self.base, members=['pc2', 'pc2'])
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_invalid_member_name_rejected(self):
        cfg = dict(self.base, members=['PC 2'])
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_member_resweep_validated(self):
        for bad in ([3, 1], [0, 2], [1, 2, 3], 'fast'):
            cfg = dict(self.base, members=['pc2'], member_resweep=bad)
            with self.assertRaises(ValueError):
                validate(cfg)
        cfg = dict(self.base, members=['pc2'], member_resweep=[2, 4])
        validate(cfg)
        self.assertEqual(cfg['member_resweep'], [2.0, 4.0])


if __name__ == '__main__':
    unittest.main()
