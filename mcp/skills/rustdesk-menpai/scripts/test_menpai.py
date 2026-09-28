#!/usr/bin/env python3
"""Unit tests for the menpai runner's pure logic: screen classification,
task-counter parsing, config validation, chain/end bookkeeping, node
selection. No I/O, no OCR — items are plain dicts."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.append(str(Path(__file__).resolve().parents[2]))
from menpai import (MENU_OPTS, TASK_BOX, MenpaiWorkflow, battle_hits, bookkeep,
                    build_screens, build_workflow, task_n_of, validate)


def item(text, x, y, score=.9):
    return dict(text=text, x=x, y=y, w=.08, h=.03, score=score)


CFG = dict(device='pc1', calibrated=True, native_resolution=[1920, 1080],
           windows=[dict(name='left', rect=[0, 0, 1000, 800], device='pc1d1'),
                    dict(name='right', rect=[0, 0, 1000, 800], device='pc1d2')],
           activity_button=[.28, 0, .1, .11], activity_list_scroll=[.45, .25, .35, .4])


def wf():
    return MenpaiWorkflow(build_screens(), build_workflow(CFG).nodes.values())


def top_bar():
    return [item('活动', .30, .05), item('排行', .37, .05), item('指引', .23, .05)]


class Classify(unittest.TestCase):
    def test_normal_map_without_menpai_task(self):
        items = top_bar() + [item('天地棋局·车迟祈雨', .80, .20), item('探索车迟神庙', .82, .24)]
        self.assertEqual(wf().classify(items), 'normal')

    def test_task_protector(self):
        items = top_bar() + [item('门派闯关(5/14)', .80, .20), item('挑战月宫护法', .80, .24)]
        self.assertEqual(wf().classify(items), 'task_protector')

    def test_task_claim(self):
        items = top_bar() + [item('门派闯关·领取任务', .80, .20), item('领取门派闯关任务', .80, .24)]
        self.assertEqual(wf().classify(items), 'task_claim')

    def test_task_frame_with_battle_cancel_stays_battle(self):
        # battle-fade transition: panel readable while the battle 取消
        # lingers — must stay battle (never click a task entry mid-fade)
        for name, subtitle in (('task_protector', '挑战月宫护法'),
                               ('task_claim', '领取门派闯关任务')):
            items = top_bar() + [item('门派闯关(5/14)', .80, .20),
                                 item(subtitle, .80, .24), item('取消', .94, .95)]
            self.assertEqual(wf().classify(items), 'battle', name)

    def test_claim_entry_with_clipped_title(self):
        # OCR often clips 门派 off the stylized title; 闯关 must suffice.
        items = top_bar() + [item('闯关(14/14)', .80, .20), item('领取任务', .80, .24)]
        self.assertEqual(wf().classify(items), 'task_claim')

    def test_messenger_menu(self):
        items = top_bar() + [item('请选择要做的事', .74, .47), item('活动说明', .78, .55),
                             item('门派乱斗', .78, .63), item('门派闯关', .78, .71)]
        self.assertEqual(wf().classify(items), 'messenger_menu')

    def test_claim_menu(self):
        items = top_bar() + [item('请选择要做的事', .74, .47), item('领取任务', .78, .60),
                             item('便捷组队', .78, .68)]
        self.assertEqual(wf().classify(items), 'claim_menu')

    def test_protector_menu(self):
        items = top_bar() + [item('请选择要做的事', .74, .62), item('进入战斗', .78, .70)]
        self.assertEqual(wf().classify(items), 'protector_menu')

    def test_activity_panel(self):
        items = [item('活动', .50, .10), item('日常活动', .12, .20),
                 item('门派争霸', .40, .25), item('参加', .55, .26)]
        self.assertEqual(wf().classify(items), 'activity_panel')

    def test_battle_toolbar(self):
        items = [item('法术', .60, .80), item('攻击', .68, .80), item('自动', .84, .80)]
        self.assertEqual(wf().classify(items), 'battle')

    def test_battle_round_cancel(self):
        items = [item('2', .48, .05), item('取消', .94, .88)]
        self.assertEqual(wf().classify(items), 'battle')

    def test_menu_misread_never_looking_ended(self):
        # Menu options visible but 请选择要做的事 misread → unknown, not normal.
        items = top_bar() + [item('进入战斗', .78, .70)]
        self.assertEqual(wf().classify(items), 'unknown')

    def test_chat_bubble_does_not_block_normal(self):
        items = top_bar() + [item('快去挑战月宫护法吧', .35, .85), item('你获得了3126点队伍长额外经验', .1, .93)]
        self.assertEqual(wf().classify(items), 'normal')

    def test_battle_transition_frame_is_not_normal(self):
        # battle starting while map elements are still readable: the strong
        # 取消 vetoes normal (misclicking the activity button mid-battle)
        items = top_bar() + [item('取消', .94, .95)]
        self.assertEqual(wf().classify(items), 'battle')


class TaskCounter(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(task_n_of([item('门派闯关(5/14)', .8, .2)]), (5, 14))
        self.assertEqual(task_n_of([item('门派闯关（13/14）', .8, .2)]), (13, 14))
        self.assertEqual(task_n_of([item('闯关1/14', .8, .2)]), (1, 14))

    def test_no_counter_on_claim_entry(self):
        self.assertEqual(task_n_of([item('门派闯关·领取任务', .8, .2)]), (None, None))
        self.assertEqual(task_n_of([]), (None, None))

    def test_ignores_other_digit_lines(self):
        self.assertEqual(task_n_of([item('竞技场22:00开启', .8, .2)]), (None, None))


class BattleHits(unittest.TestCase):
    def test_needs_evidence(self):
        self.assertFalse(battle_hits([item('法术', .6, .8), item('攻击', .68, .8)]))
        self.assertTrue(battle_hits([item('法术', .6, .8), item('攻击', .68, .8),
                                     item('道具', .74, .8)]))
        # icon-only menpai battles: a confident 取消 in the bottom-right
        # control slot is battle; a weak read is not
        self.assertTrue(battle_hits([item('取消', .94, .95)]))
        self.assertFalse(battle_hits([item('取消', .94, .95, score=.3)]))
        # 取消 outside the control slot is not battle evidence
        self.assertFalse(battle_hits([item('取消', .50, .50)]))


class Bookkeep(unittest.TestCase):
    def base(self):
        return dict(count=0, active=False, battle_seen=False, chain=False,
                    task_n=None, task_total=None, ended=None)

    def test_digit_flicker_preserves_last_known_counter(self):
        # a task_protector frame where the digits misread must not clobber
        # the last known N/14 (the end-of-activity count depends on it)
        s = self.base()
        bookkeep(s, dict(state='task_protector', task_n=14, task_total=14), 1.0, dict())
        bookkeep(s, dict(state='task_protector', task_n=None, task_total=None), 2.0, dict())
        self.assertEqual((s['task_n'], s['task_total']), (14, 14))

    def test_claim_completes_chain(self):
        s = self.base()
        cfg = dict(end_grace=30)
        t = 100.0
        bookkeep(s, dict(state='task_protector', task_n=14, task_total=14), t, cfg)
        bookkeep(s, dict(state='battle'), t+1, cfg)
        bookkeep(s, dict(state='task_claim'), t+2, cfg)
        self.assertEqual(s['count'], 1)
        self.assertFalse(s.get('chain'))
        self.assertNotIn('paused', s)

    def test_claim_without_battle_neither_counts_nor_pauses(self):
        # takeover joining at the claim screen: proceed, but don't fabricate
        # a count for battles we never observed
        s = self.base()
        s['chain'] = True
        bookkeep(s, dict(state='task_claim'), 1.0, dict(end_grace=30))
        self.assertEqual(s['count'], 0)
        self.assertNotIn('paused', s)

    def test_end_after_bare_map_grace(self):
        s = self.base()
        cfg = dict(end_grace=30)
        t = 100.0
        bookkeep(s, dict(state='battle'), t, cfg)
        bookkeep(s, dict(state='normal'), t+5, cfg)
        self.assertIsNone(s['ended'])
        bookkeep(s, dict(state='normal'), t+20, cfg)
        self.assertIsNone(s['ended'])
        bookkeep(s, dict(state='normal'), t+35, cfg)
        self.assertIn('activity over', s['ended'])

    def test_end_completes_final_chain(self):
        s = self.base()
        cfg = dict(end_grace=30)
        t = 100.0
        bookkeep(s, dict(state='task_protector', task_n=14, task_total=14), t, cfg)
        bookkeep(s, dict(state='battle'), t+1, cfg)
        bookkeep(s, dict(state='normal'), t+5, cfg)
        bookkeep(s, dict(state='normal'), t+40, cfg)
        self.assertEqual(s['count'], 1)
        self.assertIn('activity over', s['ended'])

    def test_end_mid_chain_does_not_count(self):
        s = self.base()
        cfg = dict(end_grace=30)
        bookkeep(s, dict(state='task_protector', task_n=7, task_total=14), 1.0, cfg)
        bookkeep(s, dict(state='battle'), 2.0, cfg)
        bookkeep(s, dict(state='normal'), 5.0, cfg)
        bookkeep(s, dict(state='normal'), 40.0, cfg)
        self.assertEqual(s['count'], 0)
        self.assertIn('activity over', s['ended'])

    def test_bare_map_before_any_battle_not_ended(self):
        s = self.base()
        bookkeep(s, dict(state='normal'), 1.0, dict(end_grace=30))
        bookkeep(s, dict(state='normal'), 100.0, dict(end_grace=30))
        self.assertIsNone(s['ended'])

    def test_new_chain_resets_battle_seen_on_claim_confirm(self):
        # runner-side: claim_task confirmation resets battle evidence
        s = self.base()
        s.update(battle_seen=True, chain=True)
        s['battle_seen'] = False  # what the runner does on confirmed == 'claim_task'
        s['chain'] = True
        bookkeep(s, dict(state='task_protector', task_n=1, task_total=14), 1.0, dict())
        self.assertTrue(s['chain'])


class Validate(unittest.TestCase):
    def test_ok(self):
        validate(CFG, 20)

    def test_rejects_uncalibrated(self):
        with self.assertRaises(ValueError):
            validate({**CFG, 'calibrated': False}, 20)

    def test_rejects_overlapping_windows(self):
        bad = {**CFG, 'windows': [dict(name='a', rect=[0, 0, 1000, 800], device='pc1d1'),
                                  dict(name='b', rect=[500, 0, 1000, 800], device='pc1d1')]}
        with self.assertRaises(ValueError):
            validate(bad, 20)

    def test_rejects_bad_end_grace(self):
        with self.assertRaises(ValueError):
            validate({**CFG, 'end_grace': 3}, 20)

    def test_rejects_rounds_out_of_range(self):
        with self.assertRaises(ValueError):
            validate(CFG, 51)


class Nodes(unittest.TestCase):
    def test_one_node_per_state(self):
        w = build_workflow(CFG)
        for state, node in [('normal', 'open_activity'), ('activity_panel', 'join_entry'),
                            ('messenger_menu', 'select_menpai'), ('claim_menu', 'claim_task'),
                            ('task_protector', 'goto_protector'),
                            ('protector_menu', 'enter_fight'), ('task_claim', 'reclaim_loop')]:
            got = w.nodes_for(state)
            self.assertEqual([n.id for n in got], [node], state)

    def test_no_nodes_while_battle_or_normal_after_map(self):
        w = build_workflow(CFG)
        self.assertEqual(w.nodes_for('battle'), [])
        self.assertEqual(w.nodes_for('unknown'), [])

    def test_long_navigation_timeouts(self):
        w = build_workflow(CFG)
        self.assertGreaterEqual(w.nodes['goto_protector'].timeout, 120)
        self.assertGreaterEqual(w.nodes['reclaim_loop'].timeout, 120)

    def test_expect_targets(self):
        w = build_workflow(CFG)
        self.assertIn('protector_menu', w.nodes['goto_protector'].expect)
        self.assertIn('claim_menu', w.nodes['reclaim_loop'].expect)
        self.assertIn('task_protector', w.nodes['claim_task'].expect)


if __name__ == '__main__':
    unittest.main(verbosity=1)
