"""The editor stops when its last window closes, and not on a reload."""
import http.client
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from editor import appwindow, server  # noqa: E402


def open_events(port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/api/events")
    r = c.getresponse()
    assert r.status == 200
    r.read1(64)   # the hello event
    return c


class LifetimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app, self.httpd, self.port = server.serve(os.path.join(self.tmp, "data"), 18700)
        self.stopped = threading.Event()
        self.app.lifetime = appwindow.Lifetime(self.stopped.set, grace=1.0, first_wait=30)
        self.app.lifetime.watch()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_count(self, n):
        t0 = time.time()
        while self.app.lifetime.count != n and time.time() - t0 < 5:
            time.sleep(0.05)
        self.assertEqual(self.app.lifetime.count, n)

    def test_stops_after_last_window_closes(self):
        c = open_events(self.port)
        self.wait_count(1)
        time.sleep(1.5)
        self.assertFalse(self.stopped.is_set())
        c.close()
        # the server notices on its next keep-alive write (every 4 s), then waits the grace time
        self.assertTrue(self.stopped.wait(10))

    def test_reload_keeps_running(self):
        c = open_events(self.port)
        self.wait_count(1)
        c.close()
        c2 = open_events(self.port)    # the reloaded page reconnects at once
        time.sleep(6)
        self.assertFalse(self.stopped.is_set())
        c2.close()

    def test_ping(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/ping")
        import json
        d = json.loads(c.getresponse().read())
        self.assertEqual(d["app"], "jbeam-editor")


class StopRuleTests(unittest.TestCase):
    def test_never_opened(self):
        lt = appwindow.Lifetime(lambda: None, grace=1, first_wait=5)
        self.assertFalse(lt.should_stop(lt.started + 4))
        self.assertTrue(lt.should_stop(lt.started + 6))


if __name__ == "__main__":
    unittest.main()
