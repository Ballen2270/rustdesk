"""Independent recovery geometry and residual NPC dialog checks, offline."""
import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from zhuagui import read_recovery_dialog, recovery_dialog_visible, recovery_window_aligned


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def frame(self, stale=False):
        image = Image.new('RGB', (1280, 960), (15, 45, 130))
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 10, 679, 39), fill='white')
        draw.rectangle((40, 40, 679, 519), fill=(190, 160, 95))
        blob = io.BytesIO()
        image.save(blob, format='PNG')
        return [SimpleNamespace(type='text', text=f'native_resolution=1280x960 stale={stale}'),
                SimpleNamespace(type='image', data=base64.b64encode(blob.getvalue()).decode())]

    async def test_correct_native_window_is_independently_verified(self):
        title = dict(text='梦幻西游：时空', score=1., x=60/1280, y=18/960,
                     w=100/1280, h=14/960)
        call = AsyncMock(return_value=self.frame())
        with patch('zhuagui.region_ocr', return_value=[title]):
            self.assertTrue(await recovery_window_aligned(
                dict(device='pc3', rect=[40, 40, 640, 480]), call, dict(device='pc3')))
            self.assertFalse(await recovery_window_aligned(
                dict(device='pc3', rect=[90, 40, 640, 480]), call, dict(device='pc3')))
        self.assertEqual(call.call_args.args, ('screenshot', 'pc3'))

    async def test_task_word_without_window_title_cannot_authorize_input(self):
        word = dict(text='捉鬼', score=1., x=.8, y=.22, w=.08, h=.04)
        with patch('zhuagui.region_ocr', return_value=[word]):
            self.assertFalse(await recovery_window_aligned(
                dict(rect=[40, 40, 640, 480]), AsyncMock(return_value=self.frame()), dict(device='pc3')))

    async def test_stale_or_resized_full_frame_is_rejected(self):
        for content in (self.frame(stale=True), self.frame()):
            if 'stale=False' in content[0].text:
                content[0].text = 'native_resolution=1920x1080 stale=False'
            with patch('zhuagui.region_ocr') as recognize:
                self.assertFalse(await recovery_window_aligned(
                    dict(rect=[40, 40, 640, 480]), AsyncMock(return_value=content), dict(device='pc3')))
                recognize.assert_not_called()

    def test_npc_name_and_dialog_body_are_both_required(self):
        name = dict(text='钟馗', score=1., x=.04, y=.94, w=.08, h=.03)
        body = dict(text='听闻唐时四刻冒失鬼又跑出去了', score=1., x=.3, y=.86, w=.6, h=.04)
        self.assertFalse(recovery_dialog_visible([name]))
        self.assertFalse(recovery_dialog_visible([body]))
        self.assertTrue(recovery_dialog_visible([name, body]))
        self.assertFalse(recovery_dialog_visible([dict(name, y=.4), body]))

    def test_dialog_zoom_rescues_small_name_without_lowering_confidence(self):
        name = dict(text='钟馗', score=.5, x=.04, y=.94, w=.08, h=.03)
        body = dict(text='听闻唐时四刻冒失鬼又跑出去了', score=1., x=.3, y=.86, w=.6, h=.04)
        with patch('zhuagui.region_ocr', side_effect=[[dict(name, score=.3), body], [name, body]]) as read:
            self.assertTrue(read_recovery_dialog(Image.new('RGB', (760, 570))))
            self.assertEqual([call.args[2] for call in read.call_args_list], [2, 3])

    def test_local_nameplate_fallback_still_requires_dialog_body(self):
        name = dict(text='钟馗', score=.5, x=.04, y=.94, w=.08, h=.03)
        body = dict(text='听闻唐时四刻冒失鬼又跑出去了', score=1., x=.3, y=.86, w=.6, h=.04)
        for text, expected in (([body], True), ([], False)):
            with patch('zhuagui.region_ocr', side_effect=[[], [], [name], text]) as read:
                self.assertEqual(read_recovery_dialog(Image.new('RGB', (760, 570))), expected)
                self.assertEqual(read.call_args_list[2].args[1], [0, .90, .4, .1])


if __name__ == '__main__':
    unittest.main()
