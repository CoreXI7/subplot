import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/
import http.client
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest

HOST = "subs.example.com"
os.environ["SUBS_HOST"] = HOST                                  # required: the board has no default host
ORIGIN = "https://subs.example.com"
HAS_UNIX = hasattr(socket, "AF_UNIX")
AF_UNIX = getattr(socket, "AF_UNIX", 1)
MKFIFO = getattr(os, "mkfifo", None)

BASE = tempfile.mkdtemp(prefix="subs-board-", dir=tempfile.gettempdir())
STATE = os.path.join(BASE, "state")
RULES_DIR = os.path.join(BASE, "rules")
RULES = os.path.join(RULES_DIR, "rules.json")
ENV_DIR = os.path.join(BASE, "env")
ENV = os.path.join(ENV_DIR, "subs.env")
SOCK_DIR = os.path.join(BASE, "sock")
SOCK = os.path.join(SOCK_DIR, "subs.sock")
NAV = os.path.join(BASE, "nav.json")
OVERLAY = os.path.join(BASE, "overlay")
MEDIA_ROOT = os.path.join(BASE, "media")
SHOW_DIR = os.path.join(MEDIA_ROOT, "Show")
os.environ.update(SUBS_HOME=BASE, SUBS_STATE=STATE, SUBS_RULES=RULES,
                  SUBS_ENV=ENV, SUBS_SOCK=SOCK, SUBS_NAV_FILE=NAV)


def _mkdir(path, mode=0o750):
    os.makedirs(path, mode=mode, exist_ok=True)


def _touch(path):
    _mkdir(os.path.dirname(path))
    open(path, "ab").close()


def _write_json(path, obj):
    _mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _write_bytes(path, data):
    _mkdir(os.path.dirname(path))
    with open(path, "wb") as fh:
        fh.write(data)


def _rules():
    return {
        "path_map": {"/shows": SHOW_DIR}, "media_roots": [MEDIA_ROOT],
        "exclude_libraries": ["Hentai"], "plex": {"machine_id": "0123456789abcdef0123456789abcdef"},
        "languages": {"primary": "en", "secondary": "es-419", "secondary_for_users": []},
        "thresholds": {"keep": 0.85, "review": 0.6},
        "find": {"daily_budget": 20, "min_score": 70, "sync_max_offset_ms": 1500, "retry_days": 30},
        "generate": {"model": "large-v3", "max_hours_per_night": 6, "burned_in_frames": 12,
                     "burned_in_ratio": 0.5, "es_review_until_kept": 20},
        "pc_idle": {"hours": ["00:00-09:00"], "no_processes": [], "gpu_util_below": 20},
        "priority": {"watched_days": 30, "libraries": []},
    }


for _dir in (STATE, SOCK_DIR, OVERLAY, SHOW_DIR):
    _mkdir(_dir)
_write_json(RULES, _rules())
_touch(ENV)
_write_json(NAV, [{"label": "Shows", "href": "/shows"},
                  {"label": "Docs", "href": "https://docs.example.com/"}])

import server


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__(HOST, timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _request(method, path, body=None, host=HOST, headers=None):
    conn = _UnixConnection(SOCK)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            if value is not None:
                conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        try:
            conn.endheaders(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        response = conn.getresponse()
        hdrs = {name.lower(): value for name, value in response.getheaders()}
        return response.status, hdrs, response.read()
    finally:
        conn.close()


def _send(method, path, body=None, host=HOST, headers=None):
    status, _headers, data = _request(method, path, body=body, host=host, headers=headers)
    return status, data


def _file_names(file_name):
    """Every spelling the board itself might use for a whitelisted file: the name and its url path."""
    names = [file_name]
    for key, value in server.FILES.items():
        if isinstance(value, (tuple, list)):
            fname = value[0]
        else:
            fname = value
        if fname == file_name or key == file_name:
            names.extend([fname, key])
    return list(dict.fromkeys(names))


GOOD_NAV = [
    {"label": "Shows", "href": "/shows"},
    {"label": "Sam's TV", "href": "/tv"},
    {"label": "Docs & Help", "href": "https://docs.example.com/a?b=1#c"},
    {"label": "Sub-Plot_2", "href": "/a/b"},
]
BAD_NAV = [
    {"label": "JS", "href": "javascript:alert(1)"},
    {"label": "Proto", "href": "//evil.example/x"},
    {"label": "Backslash", "href": "/a\\b"},
    {"label": "Plain", "href": "http://example.com"},
    {"label": "Bold <b>", "href": "/bold"},
    {"label": "x" * 25, "href": "/long"},
    {"label": "Ctrl\x01", "href": "/ctrl"},
    {"label": "CtrlHref", "href": "/c\x01"},
    {"label": "", "href": "/empty"},
    {"label": "Space href", "href": "/has space"},
]


@unittest.skipUnless(HAS_UNIX, "unix sockets required")
class BoardTest(unittest.TestCase):
    def setUp(self):
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        self.server = server.make_server(SOCK)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                probe = socket.socket(AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                probe.connect(SOCK)
                probe.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("server did not start on %s" % SOCK)
        server._nav = []
        server._overlay_fd = None

    def tearDown(self):
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join(timeout=5)

    def _new_overlay(self, tag):
        path = os.path.join(BASE, "ov-" + tag)
        _mkdir(path)
        return path

    def _open_overlay(self, path):
        fd = server.open_overlay(path)
        self.assertIsNotNone(fd)
        self.addCleanup(os.close, fd)
        return fd

    def test_load_nav_drops_bad_entries(self):
        """Bad labels/hrefs are dropped and the good ones survive in order."""
        entries = []
        for i in range(max(len(GOOD_NAV), len(BAD_NAV))):
            if i < len(GOOD_NAV):
                entries.append(GOOD_NAV[i])
            if i < len(BAD_NAV):
                entries.append(BAD_NAV[i])
        path = os.path.join(BASE, "nav-mixed.json")
        _write_json(path, entries)
        nav = server.load_nav(path)
        self.assertEqual([(e.get("label"), e.get("href")) for e in nav],
                         [(e["label"], e["href"]) for e in GOOD_NAV])

    def test_load_nav_at_most_eight(self):
        """Only the first eight good entries are kept."""
        path = os.path.join(BASE, "nav-many.json")
        _write_json(path, [{"label": "Tab %d" % i, "href": "/tab%d" % i} for i in range(10)])
        nav = server.load_nav(path)
        self.assertEqual([e["href"] for e in nav], ["/tab%d" % i for i in range(8)])

    def test_load_nav_non_list_is_empty(self):
        path = os.path.join(BASE, "nav-dict.json")
        _write_json(path, {"nav": GOOD_NAV})
        self.assertEqual(server.load_nav(path), [])

    def test_open_overlay_empty_is_none(self):
        self.assertIsNone(server.open_overlay(""))

    def test_open_overlay_refuses_state_rules_env_and_parents(self):
        inner = os.path.join(STATE, "inner")
        _mkdir(inner)
        for path in (STATE, RULES_DIR, ENV_DIR, inner, BASE):
            with self.assertRaises(SystemExit, msg=path):
                server.open_overlay(path)

    def test_open_overlay_refuses_symlink(self):
        link = os.path.join(BASE, "overlay-link")
        try:
            os.symlink(OVERLAY, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(OSError):
            server.open_overlay(link)

    def test_overlay_bytes_serves_whitelisted_file(self):
        data = b"<!doctype html><h1>overlay</h1>"
        _write_bytes(os.path.join(OVERLAY, "index.html"), data)
        fd = self._open_overlay(OVERLAY)
        served = [server.overlay_bytes(name, fd) for name in _file_names("index.html")]
        self.assertIn(data, served)

    def test_overlay_bytes_unknown_names_are_none(self):
        _write_bytes(os.path.join(OVERLAY, ".env"), b"SECRET=1")
        _write_bytes(os.path.join(OVERLAY, "not-whitelisted.txt"), b"secret")
        fd = self._open_overlay(OVERLAY)
        for name in (".env", "/.env", "not-whitelisted.txt", "/not-whitelisted.txt"):
            self.assertIsNone(server.overlay_bytes(name, fd), name)

    def test_overlay_bytes_refuses_symlink(self):
        folder = self._new_overlay("symlink")
        real = os.path.join(BASE, "real-subs.css")
        _write_bytes(real, b"secret")
        try:
            os.symlink(real, os.path.join(folder, "subs.css"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unavailable")
        fd = self._open_overlay(folder)
        for name in _file_names("subs.css"):
            self.assertIsNone(server.overlay_bytes(name, fd), name)

    def test_overlay_bytes_refuses_fifo(self):
        if MKFIFO is None:
            self.skipTest("mkfifo unavailable")
        folder = self._new_overlay("fifo")
        fifo = os.path.join(folder, "subs.js")
        MKFIFO(fifo, 0o640)
        fd = self._open_overlay(folder)
        writers = []

        def writer():
            try:
                writers.append(os.open(fifo, os.O_WRONLY))
            except OSError:
                pass

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for name in _file_names("subs.js"):
                self.assertIsNone(server.overlay_bytes(name, fd), name)
        finally:
            for wfd in writers:
                os.close(wfd)

    def test_overlay_bytes_refuses_oversize(self):
        folder = self._new_overlay("big")
        _write_bytes(os.path.join(folder, "subs-views.js"), b"x" * (1024 * 1024 + 1))
        fd = self._open_overlay(folder)
        for name in _file_names("subs-views.js"):
            self.assertIsNone(server.overlay_bytes(name, fd), name)

    @unittest.skipUnless(os.name == "posix", "unix socket permissions are POSIX-only")
    def test_socket_is_mode_0660(self):
        self.assertEqual(stat.S_IMODE(os.stat(SOCK).st_mode), 0o660)

    def test_get_root_ok_and_no_store(self):
        status, headers, body = _request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("no-store", headers.get("cache-control", ""))
        self.assertTrue(body)

    def test_bad_paths_are_400(self):
        for path in ("/%2e", "/../x", "//x", "/a\\b"):
            self.assertEqual(_send("GET", path)[0], 400, path)

    def test_wrong_host_is_421(self):
        self.assertEqual(_send("GET", "/", host="evil.example")[0], 421)

    def test_api_config_is_nav_only(self):
        nav = [{"label": "One", "href": "/one"},
               {"label": "Two", "href": "https://two.example/"}]
        server._nav = list(nav)
        status, _headers, body = _request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), {"nav": nav})

    def test_overlay_index_replaces_bundled(self):
        folder = self._new_overlay("index")
        data = b"<!doctype html><title>overlay</title>"
        _write_bytes(os.path.join(folder, "index.html"), data)
        fd = server.open_overlay(folder)
        self.assertIsNotNone(fd)
        saved = server._overlay_fd
        server._overlay_fd = fd

        def restore():
            server._overlay_fd = saved
            os.close(fd)

        self.addCleanup(restore)
        status, _headers, body = _request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, data)

    def test_post_over_limit_is_413(self):
        status, _headers, _body = _request(
            "POST", "/api/request", body=b"x" * (server.LIMIT + 1),
            headers={"X-Subs": "1", "Origin": ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(status, 413)

    def test_serve_without_host_raises(self):
        saved = server.HOST
        saved_env = os.environ.get("SUBS_HOST")
        server.HOST = ""
        os.environ["SUBS_HOST"] = ""
        try:
            with self.assertRaises(SystemExit):
                server.serve()
        finally:
            server.HOST = saved
            if saved_env is None:
                os.environ.pop("SUBS_HOST", None)
            else:
                os.environ["SUBS_HOST"] = saved_env


if __name__ == "__main__":
    unittest.main(verbosity=2)
