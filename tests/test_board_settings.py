import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from typing import Any, Optional

HOST = "subs.example.com"
os.environ["SUBS_HOST"] = HOST                                  # required: the board has no default host
ORIGIN = "https://subs.example.com"
HAS_UNIX = hasattr(socket, "AF_UNIX")
AF_UNIX = getattr(socket, "AF_UNIX", 1)

BASE = tempfile.mkdtemp(prefix="subs-board-settings-", dir=tempfile.gettempdir())
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
SETTINGS_DIR = os.path.join(STATE, "settings")
SETTINGS = os.path.join(SETTINGS_DIR, "settings.json")
HISTORY = os.path.join(SETTINGS_DIR, "history.log")
OLLAMA_URL = "http://10.0.0.50:11434"
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
        "pc_idle": {"hours": ["00:00-09:00"], "no_processes": [], "gpu_util_below": 20,
                    "unload_idle_model": {"ollama": OLLAMA_URL, "after_min": 10}},
        "priority": {"watched_days": 30, "libraries": []},
    }


def _reset_settings():
    """Every test starts with no page changes and the floor file back on disk."""
    _mkdir(SETTINGS_DIR)
    for name in os.listdir(SETTINGS_DIR):
        path = os.path.join(SETTINGS_DIR, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError:
            pass
    _write_json(RULES, _rules())


def _history_lines():
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            return [line for line in fh.read().splitlines() if line.strip()]
    except OSError:
        return []


for _dir in (STATE, SOCK_DIR, OVERLAY, SHOW_DIR, SETTINGS_DIR):
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


def _json(data):
    return json.loads(data.decode("utf-8")) if data else None


def _get() -> Any:
    status, body = _send("GET", "/api/settings")
    return status, _json(body), body


def _post(values: Any = None, version: int = 0, origin: str = ORIGIN,
          xsubs: Optional[str] = "1", body: Optional[bytes] = None,
          host: str = HOST) -> Any:
    if body is None:
        body = json.dumps({"version": version, "values": values}).encode("utf-8")
    headers = {"Origin": origin, "Content-Type": "application/json"}
    if xsubs is not None:
        headers["X-Subs"] = xsubs
    status, raw = _send("POST", "/api/settings", body=body, host=host, headers=headers)
    return status, _json(raw), raw


@unittest.skipUnless(HAS_UNIX, "unix sockets required")
class BoardSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        cls.server = server.make_server(SOCK)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
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

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
        finally:
            cls.server.server_close()
            cls.thread.join(timeout=5)

    def setUp(self):
        _reset_settings()

    def test_get_shape_and_no_internal_paths(self):
        """GET carries the documented keys and never leaks path_map, media_roots, the media root or the Ollama URL."""
        status, data, body = _get()
        self.assertEqual(status, 200)
        self.assertIsNotNone(data)
        for key in ("version", "at", "by", "values", "file", "effective", "ollama_set",
                    "locked_libraries", "history", "ignored", "dropped"):
            self.assertIn(key, data, key)
        self.assertEqual(data["version"], 0)
        self.assertIsNone(data["at"])
        self.assertIsNone(data["by"])
        self.assertEqual(data["values"], {})
        self.assertEqual(data["history"], [])
        self.assertEqual(data["dropped"], [])
        self.assertIsInstance(data["ollama_set"], bool)
        self.assertTrue(data["ollama_set"])
        self.assertIn("Hentai", data["locked_libraries"])
        self.assertEqual(set(data["file"]), set(data["effective"]))
        self.assertEqual(data["file"]["thresholds.keep"], 0.85)
        self.assertEqual(data["file"]["pc_idle.unload_after_min"], 10)
        self.assertEqual(data["effective"]["pc_idle.unload_after_min"], 10)
        text = body.decode("utf-8")
        for leaked in ("path_map", "media_roots", MEDIA_ROOT, OLLAMA_URL):
            self.assertNotIn(leaked, text)

    def test_post_without_xsubs_is_403(self):
        status, data, _ = _post({}, xsubs=None)
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(SETTINGS))

    def test_post_cross_origin_is_403(self):
        status, data, _ = _post({}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(SETTINGS))

    def test_post_oversize_is_413(self):
        status, data, _ = _post(body=b"x" * (64 * 1024 + 1))
        self.assertEqual(status, 413)
        self.assertFalse(os.path.exists(SETTINGS))

    def test_version_mismatch_is_409_and_writes_nothing(self):
        status, data, _ = _post({"find": {"daily_budget": 500}}, version=5)
        self.assertEqual(status, 409)
        self.assertTrue(data.get("why"))
        self.assertFalse(os.path.exists(SETTINGS))

        status, data, _ = _post({"find": {"daily_budget": 500}}, version=0)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 1)
        with open(SETTINGS, "rb") as fh:
            before = fh.read()

        status, data, _ = _post({"find": {"daily_budget": 600}}, version=0)
        self.assertEqual(status, 409)
        with open(SETTINGS, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_bad_value_names_field_and_writes_nothing(self):
        status, data, _ = _post({"find": {"daily_budget": 0}}, version=0)
        self.assertEqual(status, 400)
        self.assertEqual(data.get("field"), "find.daily_budget")
        self.assertTrue(data.get("why"))
        self.assertFalse(os.path.exists(SETTINGS))

    def test_looser_stricter_only_is_400_and_writes_nothing(self):
        status, data, _ = _post({"find": {"min_score": 69}}, version=0)
        self.assertEqual(status, 400)
        self.assertEqual(data.get("field"), "find.min_score")
        self.assertTrue(data.get("why"))
        self.assertFalse(os.path.exists(SETTINGS))

    def test_save_bumps_version_and_history_has_no_values(self):
        status, data, _ = _post({"find": {"daily_budget": 731}}, version=0)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["values"], {"find": {"daily_budget": 731}})
        self.assertEqual(len(data["history"]), 1)
        entry = data["history"][0]
        self.assertEqual(set(entry), {"at", "by", "version", "changed"})
        self.assertEqual(entry["changed"], ["find.daily_budget"])
        self.assertEqual(len(_history_lines()), 1)

        status, data, _ = _post({"find": {"daily_budget": 619}}, version=1)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 2)
        self.assertEqual(len(data["history"]), 2)
        self.assertEqual(len(_history_lines()), 2)
        for entry in data["history"]:
            self.assertEqual(set(entry), {"at", "by", "version", "changed"})
            self.assertEqual(entry["changed"], ["find.daily_budget"])

        raw = "\n".join(_history_lines())
        self.assertNotIn("731", raw)                                     # no value, in any spelling
        self.assertNotIn("619", raw)
        for line in _history_lines():
            self.assertEqual(set(json.loads(line)), {"at", "by", "version", "changed"})

    def test_empty_values_resets_effective_to_file(self):
        status, data, _ = _post({"find": {"daily_budget": 500}}, version=0)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["effective"]["find.daily_budget"], 500)
        self.assertNotEqual(data["effective"], data["file"])

        status, data, _ = _post({}, version=1)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["values"], {})
        self.assertEqual(data["effective"], data["file"])

        status, data, _ = _get()
        self.assertEqual(data["effective"], data["file"])

    def test_malformed_settings_reports_version_zero_and_accepts_zero(self):
        _write_bytes(SETTINGS, b"{not json")
        status, data, _ = _get()
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 0)

        status, data, _ = _post({"find": {"daily_budget": 500}}, version=0)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 1)

    def test_exclude_libraries_union_keeps_hentai_locked(self):
        status, data, _ = _post({"exclude_libraries": ["Hentai", "Kids"]}, version=0)
        self.assertEqual(status, 200)
        self.assertIn("Hentai", data["locked_libraries"])
        self.assertIn("Hentai", data["effective"]["exclude_libraries"])
        self.assertIn("Kids", data["effective"]["exclude_libraries"])


if __name__ == "__main__":
    unittest.main()
