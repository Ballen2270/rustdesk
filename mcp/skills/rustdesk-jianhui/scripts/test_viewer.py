"""Offline viewer API tests; never connect to a device."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from viewer import Handler, Viewer

PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63640000000600023081d02f0000000049454e44ae426082')


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix='jianhui-viewer-'))
        (cls.dir/'frames').mkdir()
        Image.new('RGB', (4, 3), 'red').save(cls.dir/'latest-left.png')
        Image.new('RGB', (4, 3), 'blue').save(cls.dir/'latest-right.png')
        Image.new('RGB', (4, 3), 'green').save(cls.dir/'frames'/'000001-left.jpg')
        (cls.dir/'state.json').write_text(json.dumps(
            dict(mode='execute', status='running', games=10,
                 config=dict(windows=[dict(name='left'), dict(name='right')]),
                 windows=[dict(count=1, active=True), dict(count=0)],
                 loop_seconds=1.2)))
        (cls.dir/'timeline.jsonl').write_text('\n'.join(json.dumps(e) for e in [
            dict(time=1.0, loop=1, window='left', state='ready', source='roi_ocr',
                 ready=[1]*4, count=1, action='match', recognition_seconds=.3, anchors=2),
            dict(time=2.0, loop=2, window='right', state='room', source='roi_ocr',
                 ready=[0]*4, count=0, recognition_seconds=.4, anchors=0)])+'\n')
        (cls.dir/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in [
            dict(time=1.5, window='left', intent='match', before='1-left-match-before.png'),
            dict(time=9.0, window='left', sent='match')])+'\n')
        Handler.viewer = Viewer(cls.dir)
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.httpd.daemon_threads = True
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path, code=200):
        with urllib.request.urlopen(self.base+path) as r:
            self.assertEqual(r.status, code)
            return r.read()

    def test_api_data_reports_artifacts_and_cursors(self):
        d = json.loads(self.get('/api/data'))
        self.assertEqual(d['tl_total'], 2)
        self.assertEqual(d['ev_total'], 2)
        self.assertEqual(d['state']['windows'][0]['count'], 1)
        self.assertEqual(d['recognition'], {})  # no recognition-*.json in fixture: tolerated
        self.assertIn('latest-left.png', d['images'])
        self.assertIn('frames/000001-left.jpg', d['images'])
        self.assertFalse(d['stopped'])
        d = json.loads(self.get('/api/data?tl_after=2&ev_after=1'))
        self.assertEqual(d['timeline'], [])
        self.assertEqual(len(d['events']), 1)

    def test_images_served_and_traversal_blocked(self):
        png = self.get('/img/latest-left.png')
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.get('/img/frames/000001-left.jpg')
        for bad in ('/img/../../state.json', '/img/..', '/img/nonexistent.png',
                    '/img/sub/dir/x.png', '/img/state.json'):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base+bad)
            self.assertEqual(ctx.exception.code, 404)

    def test_stop_creates_idempotent_stop_file(self):
        for _ in range(2):
            req = urllib.request.Request(self.base+'/api/stop', method='POST', data=b'')
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.loads(r.read()), {'stopped': True})
        self.assertTrue((self.dir/'STOP').exists())
        self.assertTrue(json.loads(self.get('/api/data'))['stopped'])

    def test_cleanup_deletes_only_frames_dir(self):
        old = self.dir/'frames'/'000000-old.jpg'
        old.write_bytes(b'\xff\xd8old')
        import os
        past = time.time()-3600
        os.utime(old, (past, past))
        fresh = self.dir/'frames'/'000002-fresh.jpg'
        Image.new('RGB', (4, 3), 'white').save(fresh)
        evidence = self.dir/'latest-left.png'
        before = evidence.stat().st_size

        req = urllib.request.Request(self.base+'/api/cleanup', method='POST',
                                     data=json.dumps(dict(max_age_seconds=60)).encode())
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read())
        self.assertEqual(d['deleted'], 1)  # only the aged frame
        self.assertGreater(d['freed_bytes'], 0)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())    # fresh frame survives max_age

        req = urllib.request.Request(self.base+'/api/cleanup', method='POST', data=b'{}')
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read())
        self.assertEqual(d['deleted'], 2)  # the fresh one + the shared fixture
        self.assertFalse(fresh.exists())
        self.assertTrue(evidence.exists())  # run-root evidence untouched
        self.assertEqual(evidence.stat().st_size, before)
        # restore the shared fixture other tests rely on (unittest runs this
        # class's tests in alphabetical order)
        Image.new('RGB', (4, 3), 'green').save(self.dir/'frames'/'000001-left.jpg')

    def test_index_serves_viewer_html(self):
        body = self.get('/')
        self.assertIn(b'\xe5\x89\x91\xe4\xbc\x9a\xe8\xa7\x82\xe5\xaf\x9f\xe5\x8f\xb0', body)  # 剑会观察台


if __name__ == '__main__':
    unittest.main()
