"""Offline selection/preflight regressions; no real devices or Vision calls."""
import base64
import copy
import contextlib
import importlib
import io
import json
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from window_targets import (build_config, identity_matches, locate_targets,
                            read_inventory, resolve_accounts, discover_windows)


def inventory(device='pc1', resolution=(1280, 960)):
    return dict(device=device, native_resolution=list(resolution), windows=[
        dict(id=str(i+1), rect=[(i % 2)*640, (i//2)*480, 640, 480])
        for i in range(4)])


def frame(data):
    image = Image.new('RGB', tuple(data['native_resolution']))
    for i, win in enumerate(data['windows']):
        x, y, w, h = win['rect']
        image.paste(Image.new('RGB', (w, h), (i+1, 0, 0)), (x, y))
    return image


def recognize(image, box, scale):
    return [dict(text='角色'+str(image.getpixel((0, 0))[0]), score=.9)]


class Fixtures:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
                with self.assertRaisesRegex(ValueError, 'independent'):
                    discover_windows(image, 'pc1')
            items = [dict(text=t, score=.9, x=.2+i*.1, y=.04, w=.06, h=.04)
                     for i, t in enumerate(('活动', '排行'))]
            with patch('window_targets.region_ocr', return_value=items):
                result = discover_windows(image, 'pc1')
                self.assertEqual(len(result['windows']), 4)

    def test_discovery_rejects_clipped_or_non_game_geometry(self):
        with patch('window_targets.layout_detector', return_value=lambda *a: ([[0, 0, 640, 300]], [])):
            with self.assertRaisesRegex(ValueError, 'boundaries'):
                discover_windows(frame(self.pc1), 'pc1')


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

    async def test_activity_mismatch_fails_before_screenshot(self):
        call = AsyncMock()
        with self.assertRaises(ValueError):
            await locate_targets(self.config(), call, self.root, 'fuben')
        call.assert_not_awaited()

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
