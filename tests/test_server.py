import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

HOST = "subs.example.com"
os.environ["SUBS_HOST"] = HOST                                  # required: the board has no default host
ORIGIN = "https://subs.example.com"
MACHINE_ID = "0123456789abcdef0123456789abcdef"
AF_UNIX = getattr(socket, "AF_UNIX")

BASE = tempfile.mkdtemp(prefix="subs-srv-", dir=tempfile.gettempdir())
STATE = os.path.join(BASE, "state")
RULES = os.path.join(BASE, "rules.json")
SOCK = os.path.join(BASE, "subs.sock")
MEDIA_ROOT = os.path.join(BASE, "media")
SHOW_DIR = os.path.join(MEDIA_ROOT, "Show")
os.environ.update(SUBS_HOME=BASE, SUBS_STATE=STATE, SUBS_RULES=RULES, SUBS_SOCK=SOCK)


def _mkdir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


def _touch(path):
    _mkdir(os.path.dirname(path))
    open(path, "ab").close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, obj):
    _mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _rules():
    return {
        "path_map": {"/shows": SHOW_DIR}, "media_roots": [MEDIA_ROOT],
        "exclude_libraries": ["Hentai"], "plex": {"machine_id": MACHINE_ID},
        "languages": {"primary": "en", "secondary": "es-419", "secondary_for_users": []},
        "thresholds": {"keep": 0.85, "review": 0.6},
        "find": {"daily_budget": 20, "min_score": 70, "sync_max_offset_ms": 1500, "retry_days": 30},
        "generate": {"model": "large-v3", "max_hours_per_night": 6, "burned_in_frames": 12,
                     "burned_in_ratio": 0.5, "es_review_until_kept": 20},
        "pc_idle": {"hours": ["00:00-09:00"], "no_processes": [], "gpu_util_below": 20},
        "priority": {"watched_days": 30, "libraries": []},
    }


for _dir in (STATE, SHOW_DIR):
    _mkdir(_dir)
_write_json(RULES, _rules())
_touch(os.path.join(SHOW_DIR, "S01E01.mkv"))
_touch(os.path.join(SHOW_DIR, "S01E02.mkv"))
_touch(os.path.join(MEDIA_ROOT, "Movie.mkv"))

import common as C
import server

LIBRARY = str(getattr(C, "LIBRARY", os.path.join(STATE, "library.json")))
COVERAGE = str(getattr(C, "COVERAGE", os.path.join(STATE, "coverage.json")))
REVIEW = str(getattr(C, "REVIEW", os.path.join(STATE, "review")))
DECISIONS = str(getattr(C, "DECISIONS", os.path.join(STATE, "decisions")))
REQUESTS = str(getattr(C, "REQUESTS", os.path.join(STATE, "requests")))

SRT = "; subplot ep-1 gen:large-v3 0.7000\n\n1\n00:00:01,000 --> 00:00:02,000\nHi\n"
REVIEW_JSON = {"key": "ep-1", "lang": "en", "source": "gen:large-v3",
               "confidence": 0.7, "preview": [[1000, 2000, "Hi"]]}


def _write_fixtures():
    _write_json(RULES, _rules())
    for _dir in (STATE, SHOW_DIR):
        _mkdir(_dir)
    for _name in ("S01E01.mkv", "S01E02.mkv"):
        _touch(os.path.join(SHOW_DIR, _name))
    _touch(os.path.join(MEDIA_ROOT, "Movie.mkv"))

    def ep(number, title):
        return {"key": "ep-%d" % number, "plex_id": number, "kind": "ep", "library": "Shows",
                "show_plex_id": 100, "show": "Show", "title": title, "season": 1,
                "episode": number, "status": "ok",
                "path": os.path.join(SHOW_DIR, "S01E0%d.mkv" % number), "need": ["en"]}

    items = {"ep-1": ep(1, "Pilot"), "ep-2": ep(2, "Episode 2"),
             "mv-5": {"key": "mv-5", "plex_id": 5, "kind": "mv", "library": "Movies",
                      "title": "Movie", "status": "ok",
                      "path": os.path.join(MEDIA_ROOT, "Movie.mkv"), "need": ["en"]}}
    _write_json(LIBRARY, {"at": _now(), "items": items})
    _write_json(COVERAGE, {"at": _now(), "stale": False, "libraries": {
        "Shows": {"items": 2, "en": 0, "es": 0, "none": 2, "image_only": 0, "generatable": 2},
        "Hentai": {"items": 9, "en": 0, "es": 0, "none": 9, "image_only": 0, "generatable": 9}}})
    _mkdir(REVIEW)
    with open(os.path.join(REVIEW, "ep-1.en.srt"), "w", encoding="utf-8") as fh:
        fh.write(SRT)
    _write_json(os.path.join(REVIEW, "ep-1.en.json"), REVIEW_JSON)


def _clear_files(directory):
    if not os.path.isdir(directory):
        return
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and name.endswith(".json"):
            try:
                os.remove(path)
            except OSError:
                pass


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


def _send(method, path, body=None, host: "str | None" = HOST, headers=None):
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
        return response.status, response.read()
    finally:
        conn.close()


def _json(data):
    return json.loads(data.decode("utf-8"))


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings(value)


class SubsServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _write_fixtures()
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
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start on %s" % SOCK)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
        finally:
            cls.server.server_close()
            cls.thread.join(timeout=5)

    def setUp(self):
        _write_fixtures()
        _clear_files(DECISIONS)
        _clear_files(REQUESTS)

    def _post(self, path, payload, extra=None):
        headers = {"X-Subs": "1", "Origin": ORIGIN, "Content-Type": "application/json"}
        headers.update(extra or {})
        return _send("POST", path, body=json.dumps(payload).encode("utf-8"), headers=headers)

    def _get(self, path):
        return _send("GET", path)

    def test_generate_now_switch(self):
        """POST /api/pc turns the owner's switch on (until = now + hours, in the state) and off (file gone); bad bodies 400."""
        import common as C
        code, data = self._post("/api/pc", {"generate_now": True, "hours": 2})
        data = _json(data)
        self.assertEqual(code, 202, data)
        self.assertTrue(os.path.exists(C.PC_OVERRIDE))
        st = _json(self._get("/api/state")[1])
        self.assertEqual(st["pc"]["generate_now_until"], data["until"])
        self.assertTrue(C.pc_override())
        for bad in ({"generate_now": "yes"}, {"generate_now": True, "hours": 0}, {"generate_now": True, "hours": 48}, {"generate_now": 1}, []):
            self.assertEqual(self._post("/api/pc", bad)[0], 400, bad)
        code, data = self._post("/api/pc", {"generate_now": False})
        self.assertEqual((code, _json(data).get("until")), (202, None))
        self.assertFalse(os.path.exists(C.PC_OVERRIDE))
        self.assertIsNone(_json(self._get("/api/state")[1])["pc"]["generate_now_until"])
        self.assertFalse(C.pc_override())

    def test_review2_server_rules(self):
        """Chunked framing refused; shift needs a real <key>.<lang> pair; NaN never emitted; a bogus review file is ignored."""
        import common as C
        self.assertEqual(_send("POST", "/api/pc", body=b'{"generate_now": true}', headers={"X-Subs": "1", "Origin": ORIGIN, "Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(_send("GET", "/api/state", headers={"Transfer-Encoding": "chunked"})[0], 400)
        _write_json(os.path.join(C.REVIEW, "ep-2.bogus.json"), {"offset_ms": 1, "step": "find"})
        _touch(os.path.join(C.REVIEW, "ep-2.bogus.srt"))
        self.assertEqual(self._post("/api/decision", {"key": "ep-2", "decision": "shift"})[0], 409)
        for n in os.listdir(C.REVIEW):
            if n.startswith("ep-1.") and n.endswith(".json"):
                m = C.read_json(os.path.join(C.REVIEW, n), 65536) or {}; m["offset_ms"] = float("inf"); C.write_json(os.path.join(C.REVIEW, n), m)
        status, data = self._get("/api/state")
        self.assertEqual(status, 200)
        row = next(r for r in _json(data)["review"] if r["key"] == "ep-1")
        self.assertIsNone(row["offset_ms"]); self.assertNotIn("Infinity", data.decode())
        self.assertEqual(self._post("/api/decision", {"key": "ep-1", "decision": "shift"})[0], 409)
        for n in os.listdir(C.REVIEW):
            if n.startswith("ep-2.bogus"): os.unlink(os.path.join(C.REVIEW, n))

    def test_server_wrong_host_421(self):
        self.assertEqual(_send("GET", "/api/state", host="evil.example")[0], 421)

    def test_server_missing_host_400(self):
        self.assertEqual(_send("GET", "/api/state", host=None)[0], 400)

    def test_server_post_without_header_403(self):
        self.assertEqual(self._post("/api/request", {"kind": "find", "key": "ep-2"},
                                    {"X-Subs": None})[0], 403)

    def test_server_cross_origin_403(self):
        self.assertEqual(self._post("/api/request", {"kind": "find", "key": "ep-2"},
                                    {"Origin": "https://evil.example"})[0], 403)

    def test_server_body_too_large_413(self):
        status, _ = _send("POST", "/api/request", body=b"x" * 9000,
                          headers={"X-Subs": "1", "Origin": ORIGIN,
                                   "Content-Type": "application/json"})
        self.assertEqual(status, 413)

    def test_server_missing_length_411(self):
        status, _ = _send("POST", "/api/request", body=None,
                          headers={"X-Subs": "1", "Origin": ORIGIN})
        self.assertEqual(status, 411)

    def test_server_get_with_body_400(self):
        self.assertEqual(_send("GET", "/api/state", body=b"hello")[0], 400)

    def test_server_method_405(self):
        self.assertEqual(_send("PUT", "/api/state")[0], 405)

    def test_state_shape(self):
        status, data = self._get("/api/state")
        self.assertEqual(status, 200)
        obj = _json(data)
        for key in ("at", "coverage", "review", "tonight", "queue", "budget", "pc"):
            self.assertIn(key, obj)
        rows = [d for d in _walk_dicts(obj.get("review")) if "plex_url" in d]
        row = next((d for d in rows if d.get("key") == "ep-1"
                    or d.get("item_key") == "ep-1"), None)
        self.assertIsNotNone(row, obj.get("review"))
        assert row is not None
        self.assertIn(MACHINE_ID, str(row.get("plex_url", "")))
        self.assertNotIn("path", row)

    def test_request_202_then_409(self):
        body = {"kind": "find", "key": "ep-2"}
        self.assertEqual(self._post("/api/request", body)[0], 202)
        self.assertEqual(self._post("/api/request", body)[0], 409)

    def test_request_404_unknown(self):
        self.assertEqual(self._post("/api/request", {"kind": "find", "key": "nope-999"})[0], 404)

    def test_decision_only_for_review_keys_except_exclude(self):
        self.assertEqual(self._post("/api/decision", {"key": "ep-2", "decision": "keep"})[0], 409)
        self.assertEqual(self._post("/api/decision", {"key": "ep-2", "decision": "exclude"})[0], 202)
        self.assertEqual(self._post("/api/decision",
                                    {"key": "ep-1", "decision": "keep", "note": "ok"})[0], 202)
        self.assertEqual(self._post("/api/decision", {"key": "ep-1", "decision": "shift"})[0], 409)   # no measured offset yet
        import common as C
        for n in os.listdir(C.REVIEW):
            if n.startswith("ep-1.") and n.endswith(".json"):
                m = C.read_json(os.path.join(C.REVIEW, n), 65536) or {}; m["offset_ms"] = -29700; C.write_json(os.path.join(C.REVIEW, n), m)
        self.assertEqual(self._post("/api/decision", {"key": "ep-1", "decision": "shift"})[0], 202)
        rows = _json(self._get("/api/state")[1])["review"]
        self.assertTrue(any(r.get("key") == "ep-1" and r.get("offset_ms") == -29700 for r in rows), rows)

    def test_missing_and_show(self):
        status, data = self._get("/api/missing?q=show")
        self.assertEqual(status, 200)
        obj = _json(data)
        rows = [d for d in _walk_dicts(obj) if d.get("missing_en") == 2]
        self.assertTrue(rows, obj)
        self.assertTrue(any("Show" in str(d) for d in rows), rows)

        status, data = self._get("/api/show/100")
        self.assertEqual(status, 200)
        obj = _json(data)
        found = set()
        for item in _walk_dicts(obj):
            if item.get("plex_id") in (1, 2):
                found.add(item["plex_id"])
            elif item.get("episode") in (1, 2):
                found.add(item["episode"])
        self.assertEqual(found & {1, 2}, {1, 2}, obj)

    def test_no_media_path_in_any_payload(self):
        for path in ("/api/state", "/api/missing?q=", "/api/show/100"):
            status, data = self._get(path)
            self.assertEqual(status, 200, path)
            text = data.decode("utf-8", "replace")
            self.assertNotIn("/media/", text)
            for value in _strings(_json(data)):
                self.assertNotIn(MEDIA_ROOT, value)
                self.assertNotIn(MEDIA_ROOT.replace("\\", "/"), value)
                self.assertNotIn("/media/", value)

    def test_no_hentai_in_any_payload(self):
        for path in ("/api/state", "/api/missing?q=", "/api/show/100"):
            status, data = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("Hentai", data.decode("utf-8", "replace"))


_ORDER = (
    "test_server_wrong_host_421 test_server_missing_host_400 "
    "test_server_post_without_header_403 test_server_cross_origin_403 "
    "test_server_body_too_large_413 test_server_missing_length_411 "
    "test_server_get_with_body_400 test_server_method_405 test_state_shape "
    "test_missing_and_show test_no_media_path_in_any_payload "
    "test_no_hentai_in_any_payload test_request_202_then_409 "
    "test_request_404_unknown test_decision_only_for_review_keys_except_exclude"
).split()


def _compare(left, right):
    try:
        return _ORDER.index(left) - _ORDER.index(right)
    except ValueError:
        return (left > right) - (left < right)


unittest.TestLoader.sortTestMethodsUsing = staticmethod(_compare)

if __name__ == "__main__":
    unittest.main(verbosity=2)
