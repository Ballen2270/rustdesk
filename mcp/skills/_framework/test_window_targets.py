"""Offline selection/preflight regressions; no real devices or Vision calls."""
import base64
import copy
import contextlib
import importlib
import io
import json
import os
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from window_targets import (TITLE_BAND_PX, build_config, cache_path,
                            covered_fraction, covered_strips, identity_matches,
                            load_cached_inventory, locate_targets, order_row_major,
                            read_inventory, resolve_accounts, save_inventory_cache,
                            discover_windows)


def inventory(device='pc1', resolution=(1280, 960)):
    return dict(device=device, native_resolution=list(resolution), windows=[
        dict(id=str(i+1), rect=[(i % 2)*640, (i//2)*480, 640, 480])
        for i in range(4)])


def frame(data):
    # Blue desktop background, like the real captures: neutral enough that a
    # black background would both form phantom title bands and hide the
    # content bottom edge from the pixel detector.
    image = Image.new('RGB', tuple(data['native_resolution']), (15, 45, 130))
    for i, win in enumerate(data['windows']):
        x, y, w, h = win['rect']
        image.paste(Image.new('RGB', (w, h), (i+1, 0, 0)), (x, y))
        if y > 0:  # window chrome above the content rect (as measured on device)
            top = max(0, y-TITLE_BAND_PX)
            # Light neutral gray: a real title band the pixel detector and
            # the cache layout fingerprint can measure (dark gray is not
            # "neutral" for _title_bands).
            image.paste(Image.new('RGB', (w, y-top), (240, 240, 240)), (x, top))
    return image


def recognize(image, box, scale):
    return [dict(text='角色'+str(image.getpixel((0, 0))[0]), score=.9)]


def fake_ocr(image, box, scale):
    """Cache verification and identity resolution on synthetic frames: light
    title bands read as the game title, colored windows as account names."""
    r = image.convert('RGB').getpixel((0, 0))[0]
    if r == 240:
        return [dict(text='梦幻西游', score=.9)]
    return [dict(text='角色'+str(r), score=.9)] if r else []


def titled_inventory(device='pc1', resolution=(1280, 960)):
    return dict(device=device, native_resolution=list(resolution), windows=[
        dict(id='1', rect=[40, 80, 560, 420]),
        dict(id='2', rect=[640, 80, 560, 420])])


class Fixtures:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Window-inventory cache is disabled by default so the offline suite
        # never reads or writes the operator's real ~/.cache.
        previous = os.environ.get('RUSTDESK_WINDOW_CACHE')
        os.environ['RUSTDESK_WINDOW_CACHE'] = ''
        self.addCleanup(self._restore_cache_env, previous)
        self.cache_dir = self.root/'cache'
        self.pc1 = inventory()
        self.pc2 = inventory('pc2', (1400, 1000))
        self.paths = []
        for data in (self.pc1, self.pc2):
            path = self.root/(data['device']+'.json')
            path.write_text(json.dumps(data))
            self.paths.append(path)
        self.template = dict(device='pc1', calibrated=False, windows=[
            dict(name='left', mode='double', double_points=200, rounds=5,
                 reward_mode_confirmed=True), dict(name='right', rounds=3)])

    def _restore_cache_env(self, previous):
        if previous is None:
            os.environ.pop('RUSTDESK_WINDOW_CACHE', None)
        else:
            os.environ['RUSTDESK_WINDOW_CACHE'] = previous

    def enable_cache(self):
        os.environ['RUSTDESK_WINDOW_CACHE'] = str(self.cache_dir)

    def config(self, activity='zhuagui'):
        return build_config(self.template, self.paths, ['pc1:4', 'pc2:2'],
                            ['角色4', '角色2'], activity)


class SelectionTests(Fixtures, unittest.TestCase):
    def test_cross_device_leaders_preserve_plans_and_resolutions(self):
        cfg = self.config()
        self.assertEqual([w['device'] for w in cfg['windows']], ['pc1', 'pc2'])
        self.assertEqual(cfg['windows'][0]['double_points'], 200)
        self.assertEqual(cfg['windows'][1]['rounds'], 3)
        self.assertEqual(cfg['windows'][1]['native_resolution'], [1400, 1000])
        self.assertTrue(all(w['role'] == 'leader' for w in cfg['windows']))
        self.assertFalse(self.template['calibrated'])

    def test_accounts_follow_identity_after_reordering(self):
        cfg = self.config()
        for win in cfg['windows']:
            win['rect'] = [0, 0, 640, 480]
        resolved = resolve_accounts(cfg, {'pc1': self.pc1, 'pc2': self.pc2},
                                    {'pc1': frame(self.pc1), 'pc2': frame(self.pc2)}, recognize)
        self.assertEqual(resolved[0]['rect'], [640, 480, 640, 480])
        self.assertEqual(resolved[1]['rect'], [640, 0, 640, 480])
        self.assertEqual(cfg['windows'][0]['rect'], [0, 0, 640, 480])

    def test_solo_selects_all_four_without_leader_or_name(self):
        cfg = build_config(self.template, self.paths, ['pc1:'+str(i) for i in range(1, 5)], [], 'mijing')
        self.assertTrue(all(w['role'] == 'account' for w in cfg['windows']))
        result = resolve_accounts(cfg, {'pc1': self.pc1}, {'pc1': frame(self.pc1)},
                                  lambda *a: self.fail('Solo selection does not require name OCR'))
        self.assertEqual(len(result), 4)

    def test_jianhui_uses_room_keyword(self):
        cfg = build_config(self.template, self.paths, ['pc1:1', 'pc1:2'],
                           ['甲的房间', '乙的房间'], 'jianhui')
        self.assertEqual([w['room_keyword'] for w in cfg['windows']], ['甲', '乙'])

    def test_menpai_rejects_more_than_two_leaders(self):
        self.assertEqual(len(self.config('menpai')['windows']), 2)
        with self.assertRaisesRegex(ValueError, 'number of selected windows'):
            build_config(self.template, self.paths, ['pc1:1', 'pc1:2', 'pc1:3'],
                         ['甲', '乙', '丙'], 'menpai')

    def test_mijing_accepts_more_than_eight_accounts(self):
        path = self.root/'pc3.json'
        path.write_text(json.dumps(inventory('pc3')))
        targets = [device+':'+str(i) for device in ('pc1', 'pc2', 'pc3')
                   for i in range(1, 5)]
        cfg = build_config(self.template, self.paths+[path], targets, [], 'mijing')
        self.assertEqual(len(cfg['windows']), 12)
        self.assertTrue(all(w['role'] == 'account' for w in cfg['windows']))
        with self.assertRaisesRegex(ValueError, 'number of selected windows'):
            build_config(self.template, self.paths, [], [], 'mijing')

    def test_duplicate_targets_and_missing_leader_identity_rejected(self):
        with self.assertRaises(ValueError):
            build_config(self.template, self.paths, ['pc1:1', 'pc1:1'], ['甲', '乙'], 'fuben')
        with self.assertRaises(ValueError):
            build_config(self.template, self.paths, ['pc1:1'], [], 'zhuagui')

    def test_clipped_inventory_rejected(self):
        self.pc1['windows'][0]['rect'][0] = -1
        self.paths[0].write_text(json.dumps(self.pc1))
        with self.assertRaises(ValueError):
            read_inventory(self.paths[0])

    def cascade(self):
        # 5-client layout: the middle-top window (id 2) is overlapped on both
        # edges by ~4.5%-wide neighbor strips — beyond the old minor-overlap
        # tolerance, fine for an occlusion-aware runner.
        return dict(device='pc1', native_resolution=[1920, 1080], windows=[
            dict(id='1', rect=[0, 0, 660, 495]),
            dict(id='2', rect=[630, 0, 660, 495]),
            dict(id='3', rect=[1260, 0, 660, 495]),
            dict(id='4', rect=[0, 495, 830, 585]),
            dict(id='5', rect=[800, 495, 830, 585])])

    def test_covered_fraction_merges_overlapping_strips(self):
        self.assertEqual(covered_fraction([0, 0, 1, 1], []), 0)
        self.assertAlmostEqual(covered_fraction([0, 0, 1, 1], [[.98, 0, .02, 1]]), .02)
        # strips sharing a corner count once, not twice
        self.assertAlmostEqual(covered_fraction([0, 0, 1, 1],
                                                [[.98, 0, .02, 1], [0, .98, 1, .02]]), .0396)
        # band coverage is relative to the band, not the window
        self.assertAlmostEqual(covered_fraction([.7, .12, .3, .24], [[.98, 0, .02, 1]]),
                               .02*.24/.072, places=4)

    def test_covered_strips_skip_the_window_itself(self):
        strips = covered_strips([630, 0, 660, 495], self.cascade(), '2')
        self.assertTrue(any(s[0] == 0 for s in strips))          # left edge strip
        self.assertTrue(any(s[0]+s[2] > .99 for s in strips))    # right edge strip
        self.assertAlmostEqual(covered_fraction([0, 0, 1, 1], strips), 60/660, places=4)

    def test_occlusion_aware_selection_maps_neighbor_strips(self):
        self.pc1 = self.cascade()
        self.paths[0].write_text(json.dumps(self.pc1))
        cfg = build_config(self.template, self.paths, ['pc1:2', 'pc2:2'],
                           ['角色2', '角色2'], 'menpai')
        resolved = resolve_accounts(cfg, {'pc1': self.pc1, 'pc2': self.pc2},
                                    {'pc1': frame(self.pc1), 'pc2': frame(self.pc2)}, recognize)
        self.assertTrue(resolved[0]['occlusion'])
        self.assertAlmostEqual(covered_fraction([0, 0, 1, 1], resolved[0]['occlusion']),
                               60/660, places=4)

    def test_strict_selection_still_rejects_neighbor_overlap(self):
        self.pc1 = self.cascade()
        self.paths[0].write_text(json.dumps(self.pc1))
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            build_config(self.template, self.paths, ['pc1:2', 'pc2:2'],
                         ['角色2', '角色2'], 'zhuagui')

    def test_mostly_hidden_leader_rejected_when_occlusion_aware(self):
        self.pc1['windows'].append(dict(id='6', rect=[60, 60, 500, 400]))
        self.paths[0].write_text(json.dumps(self.pc1))
        with self.assertRaisesRegex(ValueError, 'mostly hidden'):
            build_config(self.template, self.paths, ['pc1:6', 'pc2:2'],
                         ['角色6', '角色2'], 'menpai')

    def test_unselected_overlapping_window_is_not_ignored(self):
        self.pc1['windows'][0]['rect'] = self.pc1['windows'][3]['rect']
        self.paths[0].write_text(json.dumps(self.pc1))
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            self.config()

    def test_ambiguous_or_missing_identity_rejected(self):
        cfg = self.config()
        for reader in (lambda *a: [], lambda *a: [dict(text='角色4', score=.9)]):
            with self.assertRaisesRegex(ValueError, 'matched'):
                resolve_accounts(cfg, {'pc1': self.pc1, 'pc2': self.pc2},
                                 {'pc1': frame(self.pc1), 'pc2': frame(self.pc2)}, reader)

    def test_substring_and_low_confidence_are_not_identity(self):
        self.assertFalse(identity_matches([dict(text='角色4说话', score=1)], '角色4'))
        self.assertFalse(identity_matches([dict(text='角色4', score=.2)], '角色4'))
        self.assertTrue(identity_matches([dict(text='角 色4', score=.9)], '角色4'))

    def test_same_account_cannot_satisfy_two_targets(self):
        cfg = build_config(self.template, self.paths, ['pc1:1', 'pc1:2'], ['角色1', '角色1'], 'fuben')
        with self.assertRaisesRegex(ValueError, 'same window'):
            resolve_accounts(cfg, {'pc1': self.pc1}, {'pc1': frame(self.pc1)}, recognize)

    def test_wrong_display_or_invalid_identity_region_rejected(self):
        cfg = self.config()
        with self.assertRaisesRegex(ValueError, 'resolution'):
            resolve_accounts(cfg, {'pc1': self.pc1}, {'pc1': Image.new('RGB', (640, 480))}, recognize)
        cfg['windows'][0]['identity_box'] = [0, 0, 2, 1]
        with self.assertRaisesRegex(ValueError, 'identity_box'):
            resolve_accounts(cfg, {'pc1': self.pc1}, {'pc1': frame(self.pc1)}, recognize)

    def test_discovery_requires_independent_game_anchors(self):
        image = frame(self.pc1)
        rects = [w['rect'] for w in self.pc1['windows']]
        detector = lambda *a: (rects, [])
        with patch('window_targets.layout_detector', return_value=detector):
            with patch('window_targets.region_ocr', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'No visible game windows'):
                    discover_windows(image, 'pc1')
            items = [dict(text=t, score=.9, x=.2+i*.1, y=.04, w=.06, h=.04)
                     for i, t in enumerate(('活动', '排行'))]
            with patch('window_targets.region_ocr', return_value=items):
                result = discover_windows(image, 'pc1')
                self.assertEqual(len(result['windows']), 4)

    def test_discovery_rejects_clipped_or_non_game_geometry(self):
        with patch('window_targets.layout_detector', return_value=lambda *a: ([[0, 0, 640, 300]], [])):
            with self.assertRaisesRegex(ValueError, 'No visible game windows'):
                discover_windows(frame(self.pc1), 'pc1')


    def test_row_clustered_ids_survive_row_jitter(self):
        # pc1-style bottom row: plain (y, x) sorting flips w4/w5 when the
        # rows differ by a pixel; row clustering keeps the x order.
        base = [[2, 30, 638, 482], [646, 30, 633, 476], [1272, 250, 640, 480],
                [644, 550, 640, 482], [2, 551, 638, 481]]
        jitter = [[2, 30, 638, 482], [646, 30, 633, 476], [1272, 250, 640, 480],
                  [644, 550, 640, 482], [2, 552, 638, 480]]
        self.assertEqual([base[i][0] for i in order_row_major(base)],
                         [2, 646, 1272, 2, 644])
        self.assertEqual([jitter[i][0] for i in order_row_major(jitter)],
                         [2, 646, 1272, 2, 644])

    def test_estimated_edge_refused_for_selection_and_resolution(self):
        cfg = self.config()  # built while no window is estimated
        self.pc1['windows'][3]['estimated'] = ['right']
        self.paths[0].write_text(json.dumps(self.pc1))
        with self.assertRaisesRegex(ValueError, 'estimated'):
            build_config(self.template, self.paths, ['pc1:4', 'pc2:2'],
                         ['角色4', '角色2'], 'zhuagui')
        with self.assertRaisesRegex(ValueError, 'estimated'):
            resolve_accounts(cfg, {'pc1': self.pc1, 'pc2': self.pc2},
                             {'pc1': frame(self.pc1), 'pc2': frame(self.pc2)}, recognize)
        self.pc1['windows'][3]['estimated'] = ['bogus']
        self.paths[0].write_text(json.dumps(self.pc1))
        with self.assertRaises(ValueError):
            read_inventory(self.paths[0])


def five_window_inventory(device='pc1'):
    """Five banded windows the pixel detector can measure exactly (top band
    rows never overlap the window above)."""
    rects = [[0, 80, 600, 450], [660, 80, 600, 450], [1320, 80, 600, 450],
             [0, 620, 600, 450], [660, 620, 600, 450]]
    return dict(device=device, native_resolution=[1920, 1080],
                windows=[dict(id=str(i+1), rect=r) for i, r in enumerate(rects)])


class InventoryCacheTests(Fixtures, unittest.TestCase):
    def test_cache_roundtrip_verifies_title_anchor(self):
        self.enable_cache()
        inv = titled_inventory()
        image = frame(inv)
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            save_inventory_cache('pc1', inv, image)
            loaded = load_cached_inventory(image, 'pc1')
        self.assertEqual([w['rect'] for w in loaded['windows']],
                         [w['rect'] for w in inv['windows']])
        self.assertEqual(loaded['native_resolution'], [1280, 960])

    def test_top_edge_window_cache_uses_partial_title_band(self):
        self.enable_cache()
        inv = titled_inventory()
        for win in inv['windows']:
            win['rect'][1] = 31  # title bar nearly flush with the screen top
        image = frame(inv)
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            save_inventory_cache('pc1', inv, image)
            loaded = load_cached_inventory(image, 'pc1')
        self.assertEqual([w['rect'] for w in loaded['windows']],
                         [w['rect'] for w in inv['windows']])

    def test_cache_rejected_without_title_anchor(self):
        self.enable_cache()
        inv = titled_inventory()
        save_inventory_cache('pc1', inv, frame(inv))
        plain = Image.new('RGB', (1280, 960))  # nothing at the cached rects
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            self.assertIsNone(load_cached_inventory(plain, 'pc1'))

    def test_cache_rejected_on_resolution_change(self):
        self.enable_cache()
        inv = titled_inventory()
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            save_inventory_cache('pc1', inv, frame(inv))
            self.assertIsNone(load_cached_inventory(
                Image.new('RGB', (1920, 1080)), 'pc1'))

    def test_cache_rejected_on_moved_window(self):
        self.enable_cache()
        inv = titled_inventory()
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            save_inventory_cache('pc1', inv, frame(inv))
        moved = titled_inventory()
        for win in moved['windows']:
            win['rect'][1] += 120  # windows moved down: old bands are empty
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            self.assertIsNone(load_cached_inventory(frame(moved), 'pc1'))

    def test_disabled_cache_writes_nothing_and_corrupt_cache_falls_back(self):
        inv = titled_inventory()
        save_inventory_cache('pc1', inv, frame(inv))  # disabled: no file, no error
        self.assertIsNone(cache_path('pc1'))
        self.enable_cache()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir/'pc1.windows.json').write_text('{not json')
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            self.assertIsNone(load_cached_inventory(frame(inv), 'pc1'))

    def test_partial_cache_never_loads_as_complete(self):
        # The core anti-poisoning rule: a 4-of-5 discovery persisted earlier
        # must not shorten later runs — the fifth window is still measured on
        # the frame, so the layout fingerprint fails and discovery re-runs.
        self.enable_cache()
        five = five_window_inventory()
        image = frame(five)
        partial = dict(five, windows=five['windows'][:4])
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            save_inventory_cache('pc1', partial, image)
            self.assertIsNone(load_cached_inventory(image, 'pc1'))
            save_inventory_cache('pc1', five, image)
            loaded = load_cached_inventory(image, 'pc1')
        self.assertEqual([w['rect'] for w in loaded['windows']],
                         [w['rect'] for w in five['windows']])

    def test_unconfirmed_or_estimated_windows_are_never_cached(self):
        self.enable_cache()
        five = five_window_inventory()
        five['windows'][0]['unconfirmed'] = True
        five['windows'][1]['estimated'] = ['right']
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            save_inventory_cache('pc1', five, frame(five))
        data = json.loads((self.cache_dir/'pc1.windows.json').read_text())
        self.assertEqual([w['id'] for w in data['windows']], ['3', '4', '5'])

    def test_cache_validation_ocr_error_is_a_miss_not_a_failure(self):
        self.enable_cache()
        five = five_window_inventory()
        image = frame(five)
        with patch('window_targets.region_ocr', side_effect=fake_ocr):
            save_inventory_cache('pc1', five, image)
        with patch('window_targets.region_ocr',
                   side_effect=RuntimeError('CRImageReaderError.e5rtError')):
            self.assertIsNone(load_cached_inventory(image, 'pc1'))


class PreflightTests(Fixtures, unittest.IsolatedAsyncioTestCase):
    def response(self, device, stale=False):
        data = self.pc1 if device == 'pc1' else self.pc2
        buf = io.BytesIO()
        frame(data).save(buf, format='PNG')
        w, h = data['native_resolution']
        return [SimpleNamespace(type='text', text=f'native_resolution={w}x{h} stale={stale} frame_seq=1'),
                SimpleNamespace(type='image', data=base64.b64encode(buf.getvalue()).decode())]

    async def test_preflight_only_reads_and_disables_nearest_position_remap(self):
        cfg = self.config()
        call = AsyncMock(side_effect=lambda name, device, **kwargs: self.response(device))
        import window_targets
        real_resolve = window_targets.resolve_accounts
        with patch('window_targets.discover_windows', side_effect=lambda image, device: self.pc1 if device == 'pc1' else self.pc2), patch('window_targets.resolve_accounts', side_effect=lambda *a: real_resolve(*a, recognize)):
            await locate_targets(cfg, call, self.root, 'zhuagui')
        self.assertFalse(cfg['autocal'])
        self.assertEqual({c.args[0] for c in call.await_args_list}, {'screenshot'})
        self.assertTrue((self.root/'selected-targets.json').exists())
        self.assertEqual(cfg['windows'][1]['native_resolution'], [1400, 1000])

    async def test_legacy_config_makes_no_extra_calls(self):
        call = AsyncMock()
        await locate_targets(self.template, call, self.root, 'fuben')
        call.assert_not_awaited()

    async def test_stale_frame_never_reassigns_config(self):
        cfg = self.config()
        before = copy.deepcopy(cfg)
        call = AsyncMock(side_effect=lambda name, device, **kwargs: self.response(device, True))
        with self.assertRaisesRegex(ValueError, 'fresh'):
            await locate_targets(cfg, call, self.root, 'zhuagui')
        self.assertEqual(cfg, before)

    async def test_stale_first_frame_is_retried_on_a_fresh_one(self):
        # Cold connect: the first frame can be stale; startup must retry and
        # succeed on the second attempt instead of aborting the run.
        cfg = self.config()
        queue = {device: [self.response(device, True), self.response(device)]
                 for device in ('pc1', 'pc2')}

        async def call(name, device, **kwargs):
            return queue[device].pop(0)

        import window_targets
        real_resolve = window_targets.resolve_accounts
        with patch('window_targets.discover_windows',
                   side_effect=lambda image, device: self.pc1 if device == 'pc1' else self.pc2), \
             patch('window_targets.resolve_accounts',
                   side_effect=lambda *a: real_resolve(*a, recognize)):
            await locate_targets(cfg, call, self.root, 'zhuagui')
        self.assertFalse(cfg['autocal'])
        self.assertEqual(cfg['windows'][0]['rect'], [640, 480, 640, 480])
        self.assertTrue(all(not pending for pending in queue.values()))
        self.assertTrue((self.root/'startup/pc1.attempt2.png').exists())
        self.assertTrue((self.root/'startup/pc1.discovery.json').exists())

    async def test_short_first_frame_retries_and_takes_the_best_attempt(self):
        # The startup overlay panel hides a window on the first frame and is
        # gone on the second: the best attempt (most windows) must win even
        # though the first also produced a valid inventory.
        cfg = self.config()
        short = copy.deepcopy(self.pc1)
        short['windows'] = short['windows'][:3]
        seen = []

        def discover(image, device):
            if device != 'pc1':
                return self.pc2
            if seen:
                return self.pc1
            seen.append(1)
            return short

        import window_targets
        real_resolve = window_targets.resolve_accounts
        call = AsyncMock(side_effect=lambda name, device, **kwargs: self.response(device))
        with patch('window_targets.discover_windows', side_effect=discover), \
             patch('window_targets.resolve_accounts',
                   side_effect=lambda *a: real_resolve(*a, recognize)):
            await locate_targets(cfg, call, self.root, 'zhuagui')
        self.assertEqual(cfg['windows'][0]['rect'], [640, 480, 640, 480])

    def test_vision_outage_degrades_to_unconfirmed_pixel_bands(self):
        # With Vision down the pixel bands still measure every window; they
        # are kept marked unconfirmed (never cached) instead of aborting.
        from vision import VisionUnavailable
        with patch('window_targets.region_ocr', side_effect=VisionUnavailable('down')):
            inventory = discover_windows(frame(five_window_inventory()), 'pc1')
        self.assertEqual(len(inventory['windows']), 5)
        self.assertTrue(all(w['unconfirmed'] for w in inventory['windows']))
        self.assertTrue(all(not w['estimated'] for w in inventory['windows']))

    async def test_activity_mismatch_fails_before_screenshot(self):
        call = AsyncMock()
        with self.assertRaises(ValueError):
            await locate_targets(self.config(), call, self.root, 'fuben')
        call.assert_not_awaited()

    async def test_verified_cache_skips_full_discovery_until_layout_moves(self):
        self.enable_cache()
        self.pc1 = titled_inventory()
        self.paths[0].write_text(json.dumps(self.pc1))
        cfg = build_config(self.template, self.paths, ['pc1:1', 'pc2:2'],
                           ['角色1', '角色2'], 'zhuagui')
        call = AsyncMock(side_effect=lambda name, device, **kwargs: self.response(device))
        import window_targets
        real_resolve = window_targets.resolve_accounts

        async def run(output, discover):
            output.mkdir(parents=True)  # runners create the run dir before preflight
            with patch('window_targets.region_ocr', side_effect=fake_ocr), \
                 patch('window_targets.discover_windows', side_effect=discover), \
                 patch('window_targets.resolve_accounts',
                       side_effect=lambda *a: real_resolve(*a, recognize=fake_ocr)):
                await locate_targets(cfg, call, output, 'zhuagui')

        # run 1: no cache yet -> discovery produces the layout and seeds it
        await run(self.root/'run1', lambda image, device: self.pc1 if device == 'pc1' else self.pc2)
        self.assertTrue((self.cache_dir/'pc1.windows.json').exists())
        self.assertEqual(cfg['windows'][0]['rect'], [40, 80, 560, 420])

        # run 2: pc1's cached rects verify on the fresh frame -> no discovery
        def fail_pc1(image, device):
            if device == 'pc1':
                raise AssertionError('verified cache must skip full discovery')
            return self.pc2
        await run(self.root/'run2', fail_pc1)
        self.assertEqual(cfg['windows'][0]['rect'], [40, 80, 560, 420])

        # run 3: windows moved -> cached title bands fail -> rediscovery
        moved = copy.deepcopy(self.pc1)
        for win in moved['windows']:
            win['rect'][1] += 120
        self.pc1 = moved
        calls = []

        def discover_moved(image, device):
            calls.append(device)
            return moved if device == 'pc1' else self.pc2
        await run(self.root/'run3', discover_moved)
        self.assertEqual(set(calls), {'pc1', 'pc2'})  # pc2 has no cache (y=0 fixture)
        self.assertEqual(cfg['windows'][0]['rect'], [40, 200, 560, 420])

    async def test_all_five_runners_selected_startup_and_stop_without_inputs(self):
        root = Path(__file__).resolve().parents[3]
        self.pc1['device'], self.pc2['device'] = 'mocka', 'mockb'
        for path, data in zip(self.paths, (self.pc1, self.pc2)):
            path.write_text(json.dumps(data))
        spec = self.root/'mock-spec.json'
        spec.write_text(json.dumps(dict(mcpServers=dict(rustdesk=dict(command='offline-mock', args=[])))))
        calls = []

        async def tool(name, arguments):
            calls.append((name, arguments['device']))
            self.assertEqual(name, 'screenshot')
            device = 'pc1' if arguments['device'] == 'mocka' else 'pc2'
            return SimpleNamespace(is_error=False, content=self.response(device))

        session = SimpleNamespace(initialize=AsyncMock(), call_tool=tool)

        @contextlib.asynccontextmanager
        async def open_mock(*args, **kwargs):
            yield session

        @contextlib.asynccontextmanager
        async def stdio_mock(*args, **kwargs):
            yield (None, None)

        sys.path.insert(0, str(root/'mcp/skills'))
        self.addCleanup(sys.path.remove, str(root/'mcp/skills'))
        import _framework.window_targets as hooked
        for activity in ('zhuagui', 'fuben', 'menpai', 'mijing', 'jianhui'):
            with self.subTest(activity=activity):
                scripts = root/'mcp/skills'/('rustdesk-'+activity)/'scripts'
                sys.path.insert(0, str(scripts))
                try:
                    module = importlib.import_module(activity)
                finally:
                    sys.path.remove(str(scripts))
                examples = scripts.parent/'assets'
                template_path = examples/('mijing.example.json' if activity == 'mijing'
                                           else 'menpai.example.json' if activity == 'menpai'
                                           else 'pc1.example.json')
                template = json.loads(template_path.read_text())
                template['members'] = ['mockb'] if activity == 'fuben' else []
                cfg = build_config(template, self.paths, ['mocka:4', 'mockb:2'],
                                   ['角色4', '角色2'], activity)
                config_path = self.root/(activity+'.json')
                config_path.write_text(json.dumps(cfg))
                output = self.root/activity
                args = SimpleNamespace(config=config_path, repo=root, output=output,
                                       execute=False, viewer=False, rounds=0, games=10,
                                       minutes=0, mcp_spec=spec, keep_frames='changes',
                                       resume_state=None, frame_interval=5)

                def resolve_and_stop(*args):
                    windows = resolve_accounts(*args, recognize)
                    (output/'STOP').touch()
                    return windows

                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch('fcntl.flock'))
                    stack.enter_context(patch.object(hooked, 'resolve_accounts', side_effect=resolve_and_stop))
                    stack.enter_context(patch.object(hooked, 'discover_windows', side_effect=lambda image, device: self.pc1 if device == 'mocka' else self.pc2))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    if activity == 'jianhui':
                        stack.enter_context(patch('mcp.client.stdio.stdio_client', stdio_mock))
                        stack.enter_context(patch('mcp.ClientSession', open_mock))
                    else:
                        stack.enter_context(patch.object(module, 'open_session', open_mock))
                    if activity == 'fuben':
                        create_task = stack.enter_context(patch.object(module.asyncio, 'create_task'))
                    self.assertEqual(await module.run(args), 0)
                    if activity == 'fuben':
                        create_task.assert_not_called()
                state = json.loads((output/'state.json').read_text())
                self.assertEqual(state['status'], 'stopped')
                self.assertEqual(state['config']['windows'][0]['rect'], [640, 480, 640, 480])
                self.assertTrue((output/'selected-targets.json').exists())
                self.assertTrue((output/'startup/mocka.png').exists())
        self.assertEqual(len(calls), 10)


if __name__ == '__main__':
    unittest.main()
