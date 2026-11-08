"""The board answers on several threads (2026-11-08): two identical requests at the same moment queue one."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot")]
import os, tempfile, threading, time, unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="subs-threads-test-", dir=os.environ.get("SUBS_SANDBOX") if os.path.isdir(os.environ.get("SUBS_SANDBOX", "")) else None)
os.environ.update(SUBS_STATE=TMP, SUBS_HOST="subs.example")
import common as C  # noqa: E402
import server  # noqa: E402


class Threads(unittest.TestCase):
    def test_parallel_identical_requests_queue_once(self):
        os.makedirs(C.REQUESTS, exist_ok=True)
        lib = {"items": {"ep-1": {"key": "ep-1", "library": "Shows", "status": "ok"}}}
        real_link = C.link_request

        def slow_link(path, obj):
            time.sleep(0.3)                     # widen the gap between the checks and the write
            return real_link(path, obj)
        out = []
        with mock.patch.object(server, "_library", lambda: (lib, {})), mock.patch.object(C, "link_request", slow_link):
            ts = [threading.Thread(target=lambda: out.append(server.handle_request({"kind": "find", "key": "ep-1"})[0]))
                  for _ in range(4)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
        self.assertEqual(sorted(out), [202, 409, 409, 409], out)
        self.assertEqual(len([n for n in os.listdir(C.REQUESTS) if n.endswith(".json")]), 1)


if __name__ == "__main__":
    unittest.main()
