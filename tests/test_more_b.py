"""Tests for the owner override, PC idle rules, progress, IMDb ids, HTTP pacing and lexical paths."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/

import calendar, datetime
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TMPHOME = os.path.realpath(tempfile.mkdtemp(prefix="subs-more-b-"))
os.environ["SUBS_HOME"] = TMPHOME
os.environ["SUBS_STATE"] = os.path.join(TMPHOME, "state")
os.environ["SUBS_RULES"] = os.path.join(TMPHOME, "rules.json")
os.environ["SUBS_FFMPEG"] = "/bin/false"
os.environ["SUBS_FFPROBE"] = "/bin/false"
os.makedirs(os.environ["SUBS_STATE"], mode=0o700, exist_ok=True)

import common as C  # noqa: E402
import find  # noqa: E402
import gen  # noqa: E402
import server  # noqa: E402


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def write_control(obj):
    parent = os.path.dirname(C.PC_OVERRIDE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    C.write_json(C.PC_OVERRIDE, obj)


class MoreTests(unittest.TestCase):
    def test_pc_override(self):
        now = datetime.datetime.utcnow()
        try:
            write_control({"until": iso(now + datetime.timedelta(seconds=30))})
            self.assertTrue(C.pc_override(now_t=calendar.timegm(now.timetuple())))
            write_control({"until": iso(now + datetime.timedelta(hours=25))})
            self.assertFalse(C.pc_override(now_t=calendar.timegm(now.timetuple())))
            write_control({"until": "not-a-timestamp"})
            self.assertFalse(C.pc_override(now_t=calendar.timegm(now.timetuple())))
        finally:
            unlink(C.PC_OVERRIDE)
        self.assertFalse(C.pc_override(now_t=calendar.timegm(now.timetuple())))

    def test_pc_idle_override_still_refuses(self):
        now = datetime.datetime(2026, 1, 1, 12, 0)
        rules = {"pc_idle": {"hours": ["00:00-23:59"], "no_processes": ["Game.exe"],
                             "gpu_util_below": 20, "vram_free_mb": 6000}}
        base = {"gpu_util": 2, "processes": ["explorer.exe"], "game_procs": [],
                "busy_procs": [], "gpu_mem_used_mb": 1000, "gpu_mem_total_mb": 32000}

        def idle(probe):
            return C.pc_idle(rules, probe, now_local=now, override=True)

        self.assertFalse(idle(dict(base, processes=["game.EXE"]))[0])
        self.assertFalse(idle(dict(base, game_procs=["other.exe"]))[0])
        self.assertFalse(idle(dict(base, gpu_util=20))[0])
        self.assertFalse(idle(dict(base, busy_procs=["python.exe"]))[0])
        self.assertFalse(idle(dict(base, gpu_mem_used_mb=30000))[0])
        no_list = dict(base)
        del no_list["game_procs"]
        ok, why = idle(no_list)
        self.assertFalse(ok)
        self.assertIn("game", why.lower())

    def test_current_payload_fresh_only_and_key_capped(self):
        path = os.path.join(C.STATE, "current.json")
        now = datetime.datetime.utcnow()

        def put(at, key="ep-1"):
            C.write_json(path, {"at": iso(at), "key": key, "step": "gen"})

        try:
            put(now - datetime.timedelta(minutes=10))
            self.assertEqual(server.current_payload({})["key"], "ep-1")
            put(now - datetime.timedelta(minutes=16))
            self.assertIsNone(server.current_payload({}))
            put(now + datetime.timedelta(hours=1))
            self.assertIsNone(server.current_payload({}))
            put(now, "k" * 130)
            self.assertEqual(server.current_payload({})["key"], "k" * 120)
            C.write_json(path, ["not", "a", "dict"])
            self.assertIsNone(server.current_payload({}))
        finally:
            unlink(path)

    def test_read_progress(self):
        def runner(rc, body):
            return lambda args, timeout: (rc, body, "")

        self.assertEqual(gen.read_progress("job", runner(0, b'{"done_ms": 5.9}')), 5)
        self.assertIsInstance(gen.read_progress("job", runner(0, b'{"done_ms": 5.9}')), int)
        self.assertIsNone(gen.read_progress("job", runner(0, b'{"done_ms": "7"}')))

        def body(size):
            base = json.dumps({"done_ms": 1, "pad": ""}, separators=(",", ":"))
            text = json.dumps({"done_ms": 1, "pad": "x" * (size - len(base))}, separators=(",", ":"))
            self.assertEqual(len(text), size)
            return text.encode()

        self.assertEqual(gen.read_progress("job", runner(0, body(4096))), 1)
        self.assertIsNone(gen.read_progress("job", runner(0, body(4097))))
        self.assertIsNone(gen.read_progress("job", runner(0, b"")))

    def test_imdb_num(self):
        self.assertEqual(find._imdb_num("tt0096697"), "96697")
        self.assertEqual(find._imdb_num("TT96697"), "96697")
        self.assertEqual(find._imdb_num("96697"), "96697")
        for bad in ("tt", "ttabc", "1234567890123", None):
            self.assertIsNone(find._imdb_num(bad))

    def test_httpclient_pacing(self):
        def response(status, headers=None):
            return (status, headers or {}, b"")

        def client(pause, responses):
            c = find.HttpClient(deadline=None, pause=pause.append)
            seq = list(responses)
            c._request = lambda *a, **k: seq.pop(0) if seq else response(200)
            return c

        pause = []
        c = client(pause, [response(200), response(200)])
        c.request("GET", "https://api.opensubtitles.com/x", {}, None, 100)
        c.request("GET", "https://api.opensubtitles.com/y", {}, None, 100)
        self.assertTrue(pause)
        self.assertGreaterEqual(pause[-1], find.MIN_INTERVAL - 1e-2)

        pause = []
        c = client(pause, [response(429, {"Retry-After": "abc"}), response(200)])
        c.request("GET", "https://api.opensubtitles.com/x", {}, None, 100)
        c.request("GET", "https://api.opensubtitles.com/y", {}, None, 100)
        self.assertGreaterEqual(max(pause or [0]), find.RETRY_DEFAULT_S - 1e-6)

        pause = []
        c = client(pause, [response(429, {"Retry-After": "0.5"}), response(200)])
        c.request("GET", "https://api.opensubtitles.com/x", {}, None, 100)
        c.request("GET", "https://api.opensubtitles.com/y", {}, None, 100)
        self.assertGreaterEqual(max(pause or [0]), 1.0 - 1e-6)

    def test_lexical_media_path(self):
        root = "/mnt/Media Root/日本語"
        rules = {"media_roots": [root]}
        self.assertTrue(C.lexical_media_path(root + "/Show 01/ep 1.mkv", rules))
        self.assertFalse(C.lexical_media_path(root, rules))
        self.assertFalse(C.lexical_media_path(root + "/../root/x.mkv", rules))
        slash = {"media_roots": [root + "/"]}
        self.assertTrue(C.lexical_media_path(root + "/ep.mkv", slash))
        self.assertFalse(C.lexical_media_path(root + "/../etc/passwd", slash))
        self.assertFalse(C.lexical_media_path(root + "Other/ep.mkv", slash))


if __name__ == "__main__":
    unittest.main()
