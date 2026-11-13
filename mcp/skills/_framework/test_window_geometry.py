"""Staggered layouts and fail-closed pixel boundary regressions."""
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from window_geometry import measure_band_windows, measure_title_windows, measure_windows
from window_targets import discover_windows


def desktop(layout=None, size=(1920, 1080), title_color='white'):
    image = Image.new('RGB', size, (15, 45, 130))
    draw = ImageDraw.Draw(image)
    rects, titles = [], []
    if layout is None:
        layout = ((10, 10, 620), (650, 10, 620), (1300, 220, 600),
                  (10, 530, 620), (650, 530, 620))
    for window in layout:
        x, y, width = window[:3]
        height = window[3] if len(window) == 4 else width*3//4
        start, end = y+30, y+30+height
        draw.rectangle((x, y, x+width-1, start-1), fill=title_color)
        draw.rectangle((x, start, x+width-1, end-1), fill=(190, 160, 95))
        rects.append([x, start, width, end-start])
        titles.append(dict(text='梦幻西游：时空', score=1.,
                           x=(x+20)/size[0], y=(y+8)/size[1],
                           w=100/size[0], h=14/size[1]))
    return image, titles, rects


class GeometryTests(unittest.TestCase):
    def test_bands_locate_stacked_clients_through_icon_gaps(self):
        image = Image.new('RGB', (1920, 1080), (15, 45, 130))
        draw = ImageDraw.Draw(image)
        windows = ((0, 2, 610, 32, 512), (652, 2, 651, 32, 512),
                   (1307, 296, 613, 325, 788), (2, 520, 650, 548, 1029),
                   (654, 522, 642, 552, 1029))
        expected = []
        for x, bar, width, top, bottom in windows:
            draw.rectangle((x, bar, x+width-1, top-1), fill=(250, 250, 250))
            draw.rectangle((x, top, x+width-1, bottom-1), fill=(190, 160, 95))
            expected.append([x, top, width, bottom-top])
        # Windows title-bar glow: the upper rows are a light gray gradient,
        # the lower rows stay white. A notification icon splits the white run
        # only where it is painted, so lower rows still span the whole window.
        draw.rectangle((2, 520, 651, 531), fill=(214, 213, 216))
        draw.rectangle((654, 522, 1295, 533), fill=(216, 215, 218))
        draw.rectangle((664, 523, 681, 537), fill=(224, 28, 31))
        self.assertEqual(sorted(measure_band_windows(image), key=lambda r: (r[1], r[0])),
                         sorted(expected, key=lambda r: (r[1], r[0])))

    def test_each_column_uses_its_own_bottom(self):
        image, titles, expected = desktop()
        self.assertEqual(measure_title_windows(image, titles), expected)

    def test_moving_right_window_does_not_cut_left_windows(self):
        image, titles, expected = desktop()
        draw = ImageDraw.Draw(image)
        draw.rectangle((1300, 220, 1899, 699), fill=(15, 45, 130))
        draw.rectangle((1300, 350, 1899, 379), fill='white')
        draw.rectangle((1300, 380, 1899, 829), fill=(190, 160, 95))
        titles[2]['y'] += 130/1080
        expected[2][1] += 130
        self.assertEqual(measure_title_windows(image, titles), expected)

    def test_missing_bottom_edge_is_not_inferred_from_aspect_ratio(self):
        image, titles, _ = desktop()
        ImageDraw.Draw(image).rectangle((1300, 250, 1899, 1079), fill=(190, 160, 95))
        self.assertIsNone(measure_title_windows(image, titles))

    def test_stronger_hud_edge_does_not_replace_outer_border(self):
        image, titles, expected = desktop()
        draw = ImageDraw.Draw(image)
        for x, start, width, height in expected:
            end = start+height
            draw.rectangle((x, end-12, x+width-1, end-7), fill='white')
        self.assertEqual(measure_title_windows(image, titles), expected)

    def test_unsupported_title_bar_keeps_legacy_fallback(self):
        image, titles, _ = desktop()
        ImageDraw.Draw(image).rectangle((1300, 220, 1899, 249), fill=(100, 150, 210))
        self.assertIsNone(measure_title_windows(image, titles))

    def test_layouts_do_not_assume_count_or_side(self):
        layouts = (
            ((900, 250, 640),),
            ((20, 20, 600), (1100, 450, 700)),
            ((300, 20, 500), (300, 480, 600)),
            ((20, 50, 500), (620, 100, 600), (1350, 350, 500)),
            tuple((20+col*620, 20+row*510, 600) for row in range(2) for col in range(3)),
            ((20, 250, 600), (650, 10, 620), (1290, 10, 620),
             (650, 530, 620), (1290, 530, 620)),
            tuple((20+col*480, 20+row*400, 450) for row in range(2) for col in range(5)),
            ((20, 20, 450, 300), (650, 200, 600, 500), (1400, 20, 650, 440)),
        )
        for layout in layouts:
            for color in ('white', (35, 35, 35)):
                with self.subTest(layout=layout, title_color=color):
                    image, titles, expected = desktop(layout, (2560, 1440), color)
                    self.assertEqual(measure_title_windows(image, list(reversed(titles))),
                                     list(reversed(expected)))

    def test_recovery_requires_independent_game_anchors(self):
        image, titles, expected = desktop()
        detector = lambda *args: ([[10, 40, 620, 180]], titles[:4])
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('指引', '排行'))]
        with patch('window_targets.layout_detector', return_value=detector):
            with patch('window_targets.region_ocr', side_effect=[titles]+[game]*5):
                inventory = discover_windows(image, 'pc1')
                self.assertEqual([w['rect'] for w in inventory['windows']],
                                 sorted(expected, key=lambda r: (r[1], r[0])))
            with patch('window_targets.layout_detector',
                       return_value=lambda *a: ([[10, 40, 620, 180]], [])):
                with patch('window_targets.region_ocr', return_value=[]):
                    with self.assertRaisesRegex(ValueError, 'No visible game windows'):
                        discover_windows(image, 'pc1')

    def test_zoom_does_not_discard_titles_read_at_native_scale(self):
        image, titles, _ = desktop()
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('活动', '排行'))]
        detector = lambda *args: ([[10, 40, 620, 180]], titles)
        with patch('window_targets.layout_detector', return_value=detector):
            with patch('window_targets.region_ocr', side_effect=[titles[:4]]+[game]*5):
                self.assertEqual(len(discover_windows(image, 'pc1')['windows']), 5)

    def test_legacy_row_overlap_error_can_recover_independent_windows(self):
        image, titles, expected = desktop()
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('活动', '排行'))]
        def detector(*args):
            raise SystemExit('windows overlap')

        with patch('window_targets.layout_detector', return_value=detector):
            with patch('window_targets.region_ocr', side_effect=[titles]+[game]*5):
                self.assertEqual([w['rect'] for w in discover_windows(image, 'pc1')['windows']],
                                 sorted(expected, key=lambda r: (r[1], r[0])))

    def test_native_and_zoom_title_duplicates_are_one_window(self):
        image, titles, expected = desktop()
        zoomed = [dict(t, y=t['y']+1/1080, h=t['h']-1/1080) for t in titles]
        self.assertEqual(measure_title_windows(image, titles+zoomed), expected)

    def test_plausible_legacy_geometry_is_checked_against_own_edges(self):
        image, titles, expected = desktop()
        incorrect = [list(r) for r in expected]
        incorrect[0][3] -= 20
        game = [dict(text=t, score=1., x=.2+i*.1, y=.04, w=.06, h=.04)
                for i, t in enumerate(('活动', '排行'))]
        with patch('window_targets.layout_detector', return_value=lambda *args: (incorrect, titles)):
            with patch('window_targets.region_ocr', side_effect=[titles]+[game]*5):
                self.assertEqual([w['rect'] for w in discover_windows(image, 'pc1')['windows']],
                                 sorted(expected, key=lambda r: (r[1], r[0])))


def clipped_startup_desktop():
    """The pc1 cold-start shape: five clients; the host connection-manager
    panel (x>=1624, y<=482) clips the right client's title bar to 352 px,
    its glyph rows splitting the bar into two pieces; taskbar 10 px below
    the bottom row's border."""
    image = Image.new('RGB', (1920, 1080), (15, 45, 130))
    draw = ImageDraw.Draw(image)
    truth = [(2, 31, 640, 480), (644, 31, 640, 480), (1272, 251, 640, 480),
             (2, 552, 640, 480), (644, 552, 640, 480)]
    for x, y, w, h in truth:
        draw.rectangle((x, y-30, x+w-1, y-1), fill=(255, 255, 255))
        draw.rectangle((x+30, y-20, x+250, y-9), fill=(40, 40, 40))    # title glyphs
        draw.rectangle((x+590, y-20, x+620, y-9), fill=(40, 40, 40))   # close icon
        draw.rectangle((x, y, x+w-1, y+h-1), fill=(190, 160, 95))
        draw.rectangle((x, y+h-2, x+w-1, y+h-1), fill=(0, 200, 120))   # bottom border
    draw.rectangle((1624, 0, 1911, 482), fill=(60, 60, 60))            # overlay panel
    draw.rectangle((0, 1042, 1919, 1079), fill=(40, 40, 40))           # taskbar
    return image, truth


def edge_error(rects, truth):
    """Worst per-edge error of a best-effort one-to-one matching."""
    if len(rects) != len(truth):
        return 10**6
    worst = 0
    remaining = [list(t) for t in truth]
    for rect in rects:
        match = min(remaining, key=lambda t: abs(t[0]-rect[0])+abs(t[1]-rect[1]))
        remaining.remove(match)
        worst = max(worst, abs(rect[0]-match[0]), abs(rect[1]-match[1]),
                    abs(rect[0]+rect[2]-match[0]-match[2]),
                    abs(rect[1]+rect[3]-match[1]-match[3]))
    return worst


class RecoveryTests(unittest.TestCase):
    def test_clipped_title_bar_is_recovered_with_measured_edges(self):
        image, truth = clipped_startup_desktop()
        windows = measure_windows(image)
        self.assertEqual(len(windows), 5)
        self.assertLessEqual(edge_error([w['rect'] for w in windows], truth), 8)
        recovered = next(w for w in windows if w['rect'][0] == 1272)
        self.assertEqual(recovered['estimated'], [])   # right edge measured, not prior
        self.assertTrue(recovered['narrow'])
        # the legacy wrapper sees the same five windows
        self.assertEqual(len(measure_band_windows(image)), 5)

    def test_dialog_inside_window_neither_hides_it_nor_becomes_a_window(self):
        def desktop(dialog):
            image = Image.new('RGB', (1920, 1080), (15, 45, 130))
            draw = ImageDraw.Draw(image)
            for x in (0, 644):
                draw.rectangle((x, 31, x+639, 79), fill=(235, 235, 235))  # title bar
                draw.rectangle((x, 80, x+639, 559), fill=(190, 160, 95))  # 640x480 content
            if dialog:
                dx, dy, dw, dh = dialog
                draw.rectangle((dx, dy, dx+dw-1, dy+29), fill=(245, 245, 245))
                draw.rectangle((dx, dy+30, dx+dw-1, dy+dh-1), fill=(60, 60, 60))
            return image

        # A welfare-style popup inside the left client: the client keeps its
        # true bottom (the aspect prior skips the dialog's bottom edge) and
        # the dialog never becomes a window of its own.
        windows = measure_windows(desktop((100, 200, 500, 330)))
        self.assertEqual(sorted(w['rect'] for w in windows),
                         [[0, 80, 640, 480], [644, 80, 640, 480]])
        # Even a dialog overhanging the client's border (its dark body hides
        # the border rows) must not drop the client or add a phantom window.
        windows = measure_windows(desktop((100, 200, 500, 400)))
        self.assertEqual(len(windows), 2)
        self.assertEqual([w['rect'][0] for w in
                          sorted(windows, key=lambda w: w['rect'][0])], [0, 644])

    def test_narrow_band_needs_title_confirmation_in_discovery(self):
        image, truth = clipped_startup_desktop()
        title = [dict(text='梦幻西游：时空', score=.9, x=.1, y=.4, w=.5, h=.2)]
        labels = [dict(text=t, score=.9, x=.2+i*.1, y=.04, w=.06, h=.04)
                  for i, t in enumerate(('活动', '排行'))]

        def ocr(crop, box, scale):
            # title strips are <=68 px tall; top-bar crops are ~72 px
            return title if crop.height <= 68 else labels

        with patch('window_targets.region_ocr', side_effect=ocr):
            inventory = discover_windows(image, 'pc1')
        self.assertEqual(len(inventory['windows']), 5)
        recovered = next(w for w in inventory['windows'] if w['rect'][0] == 1272)
        self.assertEqual(recovered['estimated'], [])

        with patch('window_targets.region_ocr', side_effect=lambda *a: []):
            with self.assertRaisesRegex(ValueError, 'No visible game windows|title'):
                # an in-game dialog's neutral bar must not slip in unconfirmed
                discover_windows(image, 'pc1')


if __name__ == '__main__':
    unittest.main()
