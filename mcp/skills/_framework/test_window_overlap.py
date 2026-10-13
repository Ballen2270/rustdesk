"""Recognition tolerates narrow edge overlap without relaxing execution guards."""
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from window_targets import build_config, minor_overlap, require_uncovered, resolve_accounts


class OverlapTests(unittest.TestCase):
    def inventory(self, second):
        return dict(device='pc1', native_resolution=[1920, 1080], windows=[
            dict(id='w1', rect=[0, 0, 640, 480]), dict(id='w2', rect=second)])

    def test_ten_pixel_edge_is_allowed_for_both_windows(self):
        inventory = self.inventory([630, 0, 640, 480])
        for window in inventory['windows']:
            require_uncovered(window, inventory)

    def test_vertical_edge_uses_window_height(self):
        inventory = self.inventory([0, 472, 640, 480])
        for window in inventory['windows']:
            require_uncovered(window, inventory)

    def test_threshold_scales_with_window_size(self):
        self.assertTrue(minor_overlap([0, 0, 320, 300], [314, 0, 320, 300]))
        self.assertFalse(minor_overlap([0, 0, 320, 300], [310, 0, 320, 300]))

    def test_deep_corner_and_large_overlap_still_rejected(self):
        for second in ([560, 430, 640, 480], [620, 0, 640, 480], [0, 0, 640, 480]):
            inventory = self.inventory(second)
            for window in inventory['windows']:
                with self.assertRaisesRegex(ValueError, 'overlapping'):
                    require_uncovered(window, inventory)

    def test_selection_and_identity_resolution_allow_minor_overlap(self):
        inventory = self.inventory([630, 0, 640, 480])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'windows.json'
            path.write_text(json.dumps(inventory))
            cfg = build_config(dict(windows=[]), [path], ['pc1:w1'], ['队长甲'], 'zhuagui')
        image = Image.new('RGB', (1920, 1080))
        image.putpixel((0, 0), (1, 0, 0))
        def recognize(crop, box, scale):
            return [dict(text='队长甲' if crop.getpixel((0, 0))[0] else '队员乙', score=1.)]
        self.assertEqual(resolve_accounts(cfg, {'pc1': inventory}, {'pc1': image}, recognize)[0]['rect'],
                         inventory['windows'][0]['rect'])
        with self.assertRaisesRegex(ValueError, 'matched 0'):
            resolve_accounts(cfg, {'pc1': inventory}, {'pc1': image}, lambda *args: [])


if __name__ == '__main__':
    unittest.main()
