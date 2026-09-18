"""Offline behavioral regression tests; never connect to a device."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from jianhui import advance, checkpoint, classify, recognize, shot_name, validate, SLOTS
from local_features import BattleAnchors


class StateTests(unittest.TestCase):
    def test_visual_fallback_only_maintains_existing_battle(self):
        anchors = BattleAnchors()
        with patch('jianhui.ocr', return_value=[]), patch.object(anchors, 'matches', return_value=True):
            img = Image.new('RGB', (960, 720))
            self.assertEqual(recognize(img, anchors=anchors, active=False)[0]['state'], 'unknown')
            obs = recognize(img, anchors=anchors, active=True)[0]
            self.assertEqual(obs['state'], 'battle')
            self.assertEqual(obs['source'], 'local_battle_anchors')

    def test_visual_memory_is_not_shared_across_windows(self):
        a, b = BattleAnchors(), BattleAnchors()
        a.references.append('sentinel')
        self.assertEqual(b.references, [])

    def feed(self, s, state, now=1, target=10):
        advance(s, {'state': state}, now, target)
        return advance(s, {'state': state}, now+1, target)

    def test_one_result_counts_once(self):
        s = {'count': 0}
        self.feed(s, 'battle')
        self.feed(s, 'result', 5)
        self.feed(s, 'result', 8)
        self.assertEqual(s['count'], 1)
        self.assertEqual(self.feed(s, 'ready', 15), 'match')

    def test_optional_panel_and_wait_for_readiness(self):
        s = {'count': 0}
        self.assertEqual(self.feed(s, 'panel'), 'close')
        self.assertIsNone(self.feed(s, 'room', 4))
        self.assertEqual(self.feed(s, 'ready', 8), 'match')
        self.assertEqual(s['count'], 0)

    def test_final_game_never_starts_eleventh(self):
        s = {'count': 9}
        self.feed(s, 'battle')
        self.feed(s, 'result', 4)
        self.assertIsNone(self.feed(s, 'ready', 8))
        self.assertEqual(s['count'], 10)

    def test_missing_result_is_not_guessed(self):
        s = {'count': 0}
        self.feed(s, 'battle')
        self.assertIsNone(self.feed(s, 'ready', 5))
        self.assertIn('paused', s)
        self.assertEqual(s['count'], 0)

    def test_pending_match_never_blindly_retried(self):
        s = {'count': 0, 'pending': 'match', 'sent': 0}
        self.assertIsNone(self.feed(s, 'ready', 2))
        self.assertIsNone(self.feed(s, 'ready', 25))
        self.assertIn('paused', s)

    def test_single_flashed_result_still_counts(self):
        s = {'count': 0}
        self.feed(s, 'battle')
        advance(s, {'state': 'result'}, 5, 10)  # one poll, then the screen auto-closes
        self.assertEqual(s['count'], 1)
        self.assertIsNone(advance(s, {'state': 'unknown'}, 6, 10))
        self.assertEqual(self.feed(s, 'ready', 15), 'match')

    def test_flashed_result_without_battle_does_not_count(self):
        s = {'count': 0}
        advance(s, {'state': 'result'}, 1, 10)
        advance(s, {'state': 'result'}, 2, 10)
        self.assertEqual(s['count'], 0)
        self.assertNotIn('active', s)

    def test_two_windows_are_independent(self):
        a, b = {'count': 0}, {'count': 0}
        self.feed(a, 'battle')
        self.feed(a, 'result', 4)
        self.assertEqual(self.feed(b, 'ready', 4), 'match')
        self.assertEqual([a['count'], b['count']], [1, 0])

    def test_long_battle_never_times_out_or_blocks_other_team(self):
        a, b = {'count': 0}, {'count': 0}
        self.feed(a, 'battle')
        for now in (1801, 14401, 1000001):
            self.assertIsNone(self.feed(a, 'battle', now))
            self.assertNotIn('paused', a)
            self.assertEqual(a['count'], 0)
            self.assertTrue(a['active'])
            self.assertEqual(self.feed(b, 'ready', now), 'match')
        self.feed(a, 'result', 1000010)
        self.assertEqual(a['count'], 1)

    def test_unknown_battle_keeps_observing_without_clicking(self):
        s = {'count': 0}
        self.feed(s, 'battle')
        self.feed(s, 'unknown', 2000)
        self.feed(s, 'unknown', 2025)
        self.assertIn('warning', s)
        self.assertNotIn('paused', s)
        self.assertEqual(s['count'], 0)
        self.feed(s, 'battle', 2050)
        self.assertNotIn('warning', s)
        self.feed(s, 'result', 2100)
        self.assertEqual(s['count'], 1)

    def test_unknown_does_not_hide_unconfirmed_input(self):
        s = {'count': 0, 'pending': 'match', 'sent': 1}
        self.feed(s, 'unknown', 30)
        self.assertIn('paused', s)

    def test_recovered_room_cannot_hide_missing_result(self):
        s = {'count': 0}
        self.feed(s, 'battle')
        self.feed(s, 'unknown', 100)
        self.feed(s, 'unknown', 200)
        self.assertIsNone(self.feed(s, 'ready', 210))
        self.assertIn('paused', s)

    def test_ten_games_with_optional_panels(self):
        s = {'count': 0}
        for game in range(10):
            start = game*100
            self.assertEqual(self.feed(s, 'ready', start), 'match')
            s.update(pending='match', sent=start+1)
            self.assertIsNone(self.feed(s, 'matching', start+3))
            self.feed(s, 'unknown', start+6)
            self.feed(s, 'battle', start+12)
            self.feed(s, 'result', start+50)
            self.assertEqual(s['count'], game+1)
            if game < 9 and game % 2 == 0:
                self.assertEqual(self.feed(s, 'panel', start+80), 'close')
                s.update(pending='close', sent=start+81)
            self.assertIsNone(self.feed(s, 'room', start+83))
        self.assertIsNone(self.feed(s, 'ready', 1005))
        self.assertNotIn('paused', s)

    def test_four_distinct_slots_required(self):
        def item(text, x, y):
            return dict(text=text, score=1, x=x, y=y, w=.02, h=.02)
        base = [item('某人的房间', .4, .03), item('房主', .1, .7), item('天梯匹配', .85, .94)]
        marks = [item('准备', x+.04, y+.04) for x, y, _, _ in SLOTS]
        self.assertEqual(classify(base+marks)['state'], 'ready')
        self.assertEqual(classify(base+marks[:3])['state'], 'room')
        self.assertEqual(classify(base+[marks[0]]*4)['state'], 'room')
        self.assertEqual(classify(base+[dict(m, text='准备中') for m in marks])['state'], 'room')

    def test_middle_owner_and_window_specific_slots(self):
        def item(text, x, y):
            return dict(text=text, score=1, x=x, y=y, w=.02, h=.02)
        slots = [[.04,.67,.17,.13],[.23,.68,.17,.14],[.60,.68,.17,.14],[.79,.65,.17,.14]]
        base = [item('甲的房间', .4,.03), item('甲', .47,.7), item('天梯匹配', .85,.94)]
        marks = [item('准备', x+.04,y+.04) for x,y,_,_ in slots]
        self.assertEqual(classify(base+marks, '甲', slots)['state'], 'ready')
        self.assertEqual(classify(base+marks, '乙', slots)['state'], 'unknown')
        self.assertEqual(classify(base+marks, '甲', SLOTS)['state'], 'room')

    def test_auto_battle_toolbar(self):
        def item(text, x, y):
            return dict(text=text, score=1, x=x, y=y, w=.02, h=.02)
        cancel = item('取消', .95,.95)
        self.assertEqual(classify([cancel, item('攻击', .85,.95)])['state'], 'battle')
        self.assertEqual(classify([cancel, item('12', .49,.03)])['state'], 'battle')
        self.assertEqual(classify([cancel])['state'], 'unknown')
        self.assertEqual(classify([cancel, item('12', .1,.03)])['state'], 'unknown')

    def test_per_window_device_validation(self):
        base = dict(device='pc1', calibrated=True, native_resolution=[1920, 1080],
                    windows=[dict(name='left', rect=[0, 0, 800, 600], room_keyword='甲'),
                             dict(name='right', rect=[900, 0, 800, 600], room_keyword='乙')])
        validate(dict(base, games=1), 1)  # baseline passes
        ok = dict(base, windows=[dict(base['windows'][0], device='pc1d1'),
                                 dict(base['windows'][1], device='pc1d2',
                                      native_resolution=[1280, 720])])
        validate(dict(ok, games=1), 1)
        for bad in ('PC1D1', 'has space', 'x/y'):
            w = [dict(base['windows'][0], device=bad), base['windows'][1]]
            with self.assertRaises(ValueError):
                validate(dict(base, windows=w, games=1), 1)
        w = [base['windows'][0], dict(base['windows'][1], native_resolution=[0, 720])]
        with self.assertRaises(ValueError):
            validate(dict(base, windows=w, games=1), 1)

    def test_room_timeout_is_configurable(self):
        strict = {'count': 0}
        advance(strict, {'state': 'room'}, 1, 10)
        advance(strict, {'state': 'room'}, 200, 10)
        self.assertIn('paused', strict)
        patient = {'count': 0}
        advance(patient, {'state': 'room'}, 1, 10, room_timeout=600)
        self.assertIsNone(advance(patient, {'state': 'room'}, 200, 10, room_timeout=600))
        self.assertNotIn('paused', patient)

    def test_shot_name_is_sortable_and_identifies_window_and_tag(self):
        self.assertRegex(shot_name('left', 'match-before'), r'^\d+-left-match-before\.png$')
        self.assertRegex(shot_name('right', 'ready'), r'^\d+-right-ready\.png$')

    def test_kick_warning_dialog_detected_over_ready_room(self):
        def item(text, x, y):
            return dict(text=text, score=1, x=x, y=y, w=.02, h=.02)
        base = [item('某人的房间', .4, .03), item('房主', .1, .7), item('天梯匹配', .85, .94)]
        marks = [item('准备', x+.04, y+.04) for x, y, _, _ in SLOTS]
        dialog = [item('倒计时：4秒', .45, .51), item('我再等等', .33, .59), item('开始匹配', .58, .59)]
        obs = classify(base+marks+dialog)
        self.assertEqual(obs['state'], 'kickwarn')
        self.assertIn('开始匹配', obs['button']['text'])
        self.assertEqual(classify(base+marks+[dialog[1]])['state'], 'ready')

    def test_kick_warning_clicks_start_match_then_confirms(self):
        s = {'count': 0}
        self.assertEqual(self.feed(s, 'kickwarn'), 'rematch')
        s.update(pending='rematch', sent=10)
        self.assertIsNone(self.feed(s, 'matching', 12))
        self.assertNotIn('pending', s)
        self.assertNotIn('paused', s)

    def test_kick_warning_during_active_battle_pauses(self):
        s = {'count': 0}
        self.feed(s, 'battle')
        self.feed(s, 'kickwarn', 5)
        self.assertIn('paused', s)
        self.assertEqual(s['count'], 0)

    def test_checkpoint_roundtrip_and_refusals(self):
        cfg = json.loads((Path(__file__).parent.parent/'assets'/'pc1.example.json').read_text())
        good = dict(status='stopped', config=cfg, games=2,
                    windows=[dict(count=1, active=False, initialized=True, pending=None),
                             dict(count=2, active=False, initialized=True)])
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d/'checkpoint.json'
            (d/'state.json').write_text(json.dumps(good))
            data = checkpoint(SimpleNamespace(state=d/'state.json', out=out))
            self.assertEqual(data['windows'][0]['count'], 1)
            self.assertEqual(data['config'], cfg)
            self.assertEqual(json.loads(out.read_text())['games'], 2)
            for mutation in ('running', 'pending', 'paused'):
                bad = json.loads(json.dumps(good))
                if mutation == 'running':
                    bad['status'] = 'running'
                elif mutation == 'pending':
                    bad['windows'][0]['pending'] = 'match'
                else:
                    bad['windows'][0]['paused'] = 'state timeout: room'
                (d/'state.json').write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    checkpoint(SimpleNamespace(state=d/'state.json', out=out))


if __name__ == '__main__':
    unittest.main()
