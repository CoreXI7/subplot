"""Unit tests (set A) for the subtitle contract: markers, SRT writing, sync windows/offsets, PC override.

Written from the brief only. Run with:  python3 test_more_a.py
No network, no PC, no media: the speech measurement is a fake module attribute.
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/

import datetime, os, re, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path: sys.path.insert(0, HERE)

TMPHOME = os.path.realpath(tempfile.mkdtemp(prefix="subs-more-a-"))
os.environ["SUBS_HOME"] = TMPHOME
os.environ["SUBS_STATE"] = os.path.join(TMPHOME, "state")
os.environ["SUBS_RULES"] = os.path.join(TMPHOME, "rules.json")
os.environ["SUBS_FFMPEG"] = "/bin/false"
os.environ["SUBS_FFPROBE"] = "/bin/false"
os.makedirs(os.environ["SUBS_STATE"], mode=0o700, exist_ok=True)

import common as C, sync, server, place, gen, find  # noqa: E402

def _txt(x): return x.decode("utf-8") if isinstance(x, bytes) else x

def _ts(ms): return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)

def _srt_bytes(cues):
    return "".join("%d\n%s --> %s\n%s\n" % (i, _ts(a), _ts(b), t)
                   for i, (a, b, t) in enumerate(cues, 1)).encode()

def _marker_line(key, source, conf):
    return f"; subplot {key} {source} {float(conf):.4f}"          # built by hand: the writer refuses bad parts

MARK_A = _marker_line("ep-1", "whisper", 0.5)
MARK_B = _marker_line("ep-2", "embed", 0.9)
BODY = "1\n00:00:01,000 --> 00:00:02,000\nhi\n"
BODYB = BODY.encode()
RULES = {"find": {"sync_max_offset_ms": 1500, "auto_shift_max_ms": 60000}}

def _measure(n):
    """A fake vad.measure that reports offset n for cues whose first start is 10000 ms (the test base) and 0 once shifted."""
    def measure(cues, spans, duration_ms, regions=None):
        off = n if cues and cues[0][0] == 10000 else 0
        return {"offset_ms": off, "halves": [off, off], "z": [5.0, 5.0], "why": ""}
    return measure

def _code(res):
    if isinstance(res, tuple): res = res[0]
    if isinstance(res, dict): res = res.get("status", res.get("code"))
    elif not isinstance(res, int): res = getattr(res, "status_code", getattr(res, "status", None))
    return res if isinstance(res, int) else -1

def _body(res):
    if isinstance(res, tuple): return res[1] if len(res) > 1 else None
    return res if isinstance(res, dict) else getattr(res, "body", None)

def _when(v):
    if isinstance(v, bool): return None
    if isinstance(v, (int, float)): return v / 1000.0 if v > 1e11 else (float(v) if v > 1e9 else None)
    if isinstance(v, str):
        try: return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError: return None
    return None

def _deadlines(*objs):
    found = []
    def walk(x):
        if isinstance(x, dict):
            for k, val in x.items():
                if "until" in str(k).lower() or "deadline" in str(k).lower():
                    t = _when(val)
                    if t is not None: found.append(t)
                walk(val)
        elif isinstance(x, (list, tuple)):
            for val in x: walk(val)
    for obj in objs:
        t = _when(obj)
        if t is not None: found.append(t)
        walk(obj)
    return found

class MoreATests(unittest.TestCase):
    def setUp(self):
        self._old_measure = sync.measure_offset

    def tearDown(self): sync.measure_offset = self._old_measure

    def test_strip_marker(self):
        m = MARK_A.encode()
        for raw in (m + b"\n" + BODYB, BODYB + m + b"\n", BODYB.replace(b"\n", b"\r\n") + m + b"\r\n"):
            out = C.strip_marker(raw)
            self.assertNotIn(m, out)
            self.assertIn(b"hi", out)
        plain = BODYB.rstrip(b"\n")
        self.assertEqual(C.strip_marker(plain).rstrip(b"\n"), plain)
        out = C.strip_marker(b"\xef\xbb\xbf" + m + b"\n" + BODYB)
        self.assertNotIn(m, out)
        self.assertTrue(out.replace(b"\xef\xbb\xbf", b"").lstrip().startswith(b"1"))

    def test_our_marker(self):
        self.assertEqual(C.our_marker(MARK_A + "\n" + BODY + MARK_B + "\n"), ("ep-2", "embed", 0.9))
        self.assertEqual(C.our_marker(MARK_A + "\n" + BODY), ("ep-1", "whisper", 0.5))
        self.assertIsNone(C.our_marker("1\n" + MARK_A + "\n00:00:01,000 --> 00:00:02,000\nhi\n"))
        bad = re.sub(r"0\.5(?:0+)?(?!\d)", "1.5", MARK_A, count=1)
        self.assertNotEqual(bad, MARK_A)
        self.assertIsNone(C.our_marker(bad))

    def test_write_srt(self):
        raw = _txt(C.write_srt([(0, 1500, "a\rb"), (2000, 3000, "c")], ("ep-2", "embed", 0.9)))
        lines = raw.rstrip("\n").splitlines()
        self.assertEqual(lines[0], "1")
        self.assertEqual(lines[-1], MARK_B)
        self.assertNotIn("\r", raw)
        self.assertNotIn("\r", raw); self.assertIn("a b", raw)   # a CR becomes a space
        self.assertIn("0.9000", MARK_B)
        for conf, tag in ((1.7, "1.0000"), (-0.5, "0.0000")):
            self.assertIn(tag, _txt(C.write_srt([(0, 1, "x")], ("ep-1", "s", conf))))

    def test_shift_cues(self):
        base = [(1000, 4000, "e"), (10000, 12000, "a"), (30000, 33000, "b")]
        out = sync.shift_cues(base, -3000)
        self.assertEqual([c[:2] for c in out], [(0, 1000), (7000, 9000), (27000, 30000)])   # a start before 0 becomes 0
        self.assertEqual(sync.shift_cues(base, -4000)[0][:2], (6000, 8000))                     # a cue ending at 0 is dropped
        self.assertEqual(sync.shift_cues([(0, 500, "x")], -400)[0][:2], (0, 100))              # never a 0-length cue
        self.assertEqual(sync.shift_cues([(0, 500, "x")], -499)[0][:2], (0, 1))

    def test_apply_timing(self):
        base = [(10000, 12000, "a"), (30000, 33000, "b"), (50000, 52000, "c")]
        sync.measure_offset = _measure(1500)
        self.assertEqual(sync.apply_timing(base, {"offset_ms": 1500}, [], 600000, RULES), (True, None, base, ""))   # in sync: nothing applied
        ok, off, cues, why = sync.apply_timing(base, {"offset_ms": 1501}, [], 600000, RULES)
        self.assertEqual((ok, off, why), (True, 1501, ""))
        self.assertEqual(cues[0][:2], (11501, 13501))                                            # the original cues, shifted
        self.assertEqual(sync.apply_timing(base, {"offset_ms": 60001}, [], 600000, RULES), (False, None, base, "off by 60.0 s"))
        self.assertEqual(sync.apply_timing(base, {"offset_ms": None, "why": "too few cues"}, [], 600000, RULES),
                         (False, None, base, "timing not confirmed (too few cues)"))
        self.assertEqual(sync.apply_timing(base, {"offset_ms": float("nan")}, [], 600000, RULES)[::3], (False, "timing not confirmed (no agreement)"))
        sync.measure_offset = lambda cs, sp, d, r=None: {"offset_ms": None, "why": "halves disagree"}    # the re-check cannot confirm
        ok, off, cues, why = sync.apply_timing(base, {"offset_ms": -3000}, [], 600000, RULES)
        self.assertEqual((ok, off, cues), (False, None, base)); self.assertEqual(why, "off by -3.0 s; shift did not fit (halves disagree)")

    def test_handle_pc(self):
        rules = {"find": {}}
        self.assertFalse(os.path.exists(C.PC_STATUS))
        res = server.handle_pc({"generate_now": False})
        self.assertEqual(_code(res), 202)
        body = _body(res)
        if isinstance(body, dict): self.assertNotIn("error", body)
        before = time.time()
        res = server.handle_pc({"generate_now": True})
        self.assertTrue(200 <= _code(res) < 300)
        found = _deadlines(_body(res), C.pc_override(), server.current_payload(rules))
        self.assertTrue(any(abs(t - (before + 6 * 3600)) <= 120 for t in found), found)
        self.assertTrue(200 <= _code(server.handle_pc({"generate_now": True, "hours": 12})) < 300)
        self.assertEqual(_code(server.handle_pc({"generate_now": True, "hours": 13})), 400)
        self.assertEqual(_code(server.handle_pc({"generate_now": True, "hours": "6"})), 400)

if __name__ == "__main__":
    unittest.main()
