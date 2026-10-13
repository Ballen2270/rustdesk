"""Offline behavioral regression tests; never connect to a device."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image, ImageDraw
from jianhui import (advance, checkpoint, classify, close_retry_needed, configured_devices,
                     merge_bands, recognize, shot_name, validate, DEFAULT_BANDS, SLOTS)
from auto_layout import find_taskbar_frac, fuzzy_in, title_band_extent
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

    def test_close_panel_can_reveal_battle_without_losing_result(self):
        s = {'count': 0, 'active': False, 'pending': 'close', 'sent': 0}
        advance(s, {'state': 'battle'}, 21, 10)
        advance(s, {'state': 'battle'}, 22, 10)
        self.assertNotIn('pending', s)
        self.assertNotIn('paused', s)
        self.assertTrue(s['active'])
        advance(s, {'state': 'result'}, 23, 10)
        advance(s, {'state': 'result'}, 24, 10)
        self.assertEqual(s['count'], 1)

    def test_close_panel_result_does_not_invent_unobserved_battle(self):
        s = {'count': 0, 'active': False, 'pending': 'close', 'sent': 0}
        advance(s, {'state': 'result'}, 21, 10)
        advance(s, {'state': 'result'}, 22, 10)
        self.assertNotIn('pending', s)
        self.assertNotIn('paused', s)
        self.assertEqual(s['count'], 0)

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

    def test_half_screen_bands_relocate_regions(self):
        # Two clients side by side on one display: button/ready marks sit at
        # different relative positions than the full-screen defaults.
        def item(text, x, y):
            return dict(text=text, score=1, x=x, y=y, w=.02, h=.02)
        bands = merge_bands(dict(bottom=[.78,.63,.22,.12], owner=[0,.40,1,.45]))
        base = [item('甲的房间', .4,.03), item('房主', .865,.51), item('天梯匹配', .91,.68)]
        self.assertEqual(classify(base, '甲', SLOTS, bands)['state'], 'room')
        self.assertEqual(classify(base, '甲', SLOTS)['state'], 'unknown')  # defaults miss the button
        self.assertIsNotNone(classify(base, '甲', SLOTS, bands)['button'])
        with self.assertRaises(ValueError):
            merge_bands(dict(nonexistent=[0,0,1,1]))
        # partial override keeps the remaining default regions intact
        self.assertEqual(merge_bands(dict(bottom=[0,.5,.2,.1]))['top'], (0, 0, 1, .30))

    def test_per_window_device_validation(self):
        base = dict(device='pc1', calibrated=True, native_resolution=[1920, 1080],
                    windows=[dict(name='left', rect=[0, 0, 800, 600], room_keyword='甲'),
                             dict(name='right', rect=[900, 0, 800, 600], room_keyword='乙')])
        validate(dict(base, games=1), 1)  # baseline passes
        ok = dict(base, windows=[dict(base['windows'][0], device='pc1d1'),
                                 dict(base['windows'][1], rect=[400, 0, 800, 600], device='pc1d2',
                                      native_resolution=[1280, 720])])
        validate(dict(ok, games=1), 1)
        # Each display has local coordinates, so identical rectangles on distinct
        # devices are valid; the per-window resolution must still bound its rect.
        local = dict(base, windows=[dict(base['windows'][0], device='pc1d1'),
                                    dict(base['windows'][0], name='right', room_keyword='乙',
                                         device='pc1d2', native_resolution=[1280, 720])])
        validate(dict(local, games=1), 1)
        with self.assertRaises(ValueError):
            validate(dict(local, games=1, windows=[dict(local['windows'][0]),
                                                   dict(local['windows'][1], rect=[600, 0, 800, 600])]), 1)
        same_device = dict(base, windows=[dict(base['windows'][0], device='pc1'),
                                          dict(base['windows'][1], rect=[400, 0, 800, 600], device='pc1')])
        with self.assertRaises(ValueError):
            validate(dict(same_device, games=1), 1)
        for bad in ('PC1D1', 'has space', 'x/y'):
            w = [dict(base['windows'][0], device=bad), base['windows'][1]]
            with self.assertRaises(ValueError):
                validate(dict(base, windows=w, games=1), 1)
        w = [base['windows'][0], dict(base['windows'][1], native_resolution=[0, 720])]
        with self.assertRaises(ValueError):
            validate(dict(base, windows=w, games=1), 1)

    def test_keep_alive_uses_only_window_devices(self):
        cfg = dict(device='pc1', windows=[dict(device='pc1d1'), dict(device='pc1d2')])
        self.assertEqual(configured_devices(cfg), ['pc1d1', 'pc1d2'])

    def test_close_retry_requires_fresh_panel_observation(self):
        state = {'pending': 'close', 'sent': 1}
        self.assertTrue(close_retry_needed(state, 'panel', 6))
        self.assertFalse(close_retry_needed(state, 'room', 6))
        self.assertFalse(close_retry_needed(state, 'unknown', 6))
        self.assertFalse(close_retry_needed(dict(state, close_esc=True), 'panel', 6))

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

    def test_kick_warning_redirects_blocked_background_click_once(self):
        s = {'count': 0, 'pending': 'match', 'sent': 1}
        self.assertEqual(self.feed(s, 'kickwarn', 3), 'rematch')
        s.update(pending='rematch', sent=5)
        self.assertIsNone(self.feed(s, 'kickwarn', 7))
        self.assertIsNone(self.feed(s, 'ready', 10))
        self.assertEqual(s['pending'], 'rematch')
        self.assertIsNone(self.feed(s, 'ready', 30))
        self.assertIn('paused', s)
        self.assertEqual(s['count'], 0)

    def test_fallback_room_rescans_ready_slots_and_modal(self):
        def item(text, x, y):
            return dict(text=text, score=1, x=x, y=y, w=.02, h=.02)
        base = [item('甲的房间', .4, .03), item('房主', .1, .7), item('天梯匹配', .85, .94)]
        dialog = [item('倒计时：4秒', .45, .51), item('我再等等', .33, .59),
                  item('开始匹配', .58, .59)]
        def scan(img, box, scale=1):
            if box in SLOTS:
                return [item('准备', box[0]+.04, box[1]+.04)]
            return dialog if box == DEFAULT_BANDS['modal'] else []
        with patch('jianhui.region_ocr', side_effect=scan), patch('jianhui.ocr', return_value=base):
            obs, _ = recognize(Image.new('RGB', (648, 487)), '甲')
        self.assertEqual(obs['state'], 'kickwarn')
        self.assertEqual(obs['ready'], [True]*4)
        self.assertEqual(obs['button']['text'], '开始匹配')

    def test_pc1_pc3_have_independent_local_coordinates(self):
        cfg = dict(device='pc1', calibrated=True, native_resolution=[1920, 1080],
                   windows=[dict(name='pc1-leader', device='pc1', rect=[8, 31, 648, 487],
                                 room_keyword='甲'),
                            dict(name='pc3-leader', device='pc3', rect=[8, 31, 648, 487],
                                 room_keyword='乙')])
        validate(cfg, 10)
        self.assertEqual(configured_devices(cfg), ['pc1', 'pc3'])

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


class AutoLayoutTests(unittest.TestCase):
    def test_fuzzy_in_tolerates_one_glyph(self):
        self.assertTrue(fuzzy_in('雾上青天', '翼上青天的房间'))   # OCR misread
        self.assertTrue(fuzzy_in('剑霜九洲', '剑霜九洲的房间'))
        self.assertFalse(fuzzy_in('雾上青天', '剑箱九洲的房间'))
        self.assertFalse(fuzzy_in('甲', '乙的房间'))

    def test_title_band_extent_on_synthetic_bar(self):
        img = Image.new('RGB', (400, 300), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 10, 349, 30], fill=(240, 240, 240))
        anchor = dict(x=.15, y=.066, w=.2, h=.026)
        self.assertEqual(title_band_extent(img, 400, 300, anchor)[0], 50)
        self.assertTrue(345 <= title_band_extent(img, 400, 300, anchor)[1] <= 350)
        dark = dict(x=.15, y=.2, w=.2, h=.026)
        self.assertIsNone(title_band_extent(img, 400, 300, dark))

    def test_find_taskbar_frac_synthetic(self):
        img = Image.new('RGB', (400, 300), (10, 10, 10))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 280, 400, 300], fill=(200, 200, 200))
        self.assertTrue(.9 < find_taskbar_frac(img) < .97)


if __name__ == '__main__':
    unittest.main()
