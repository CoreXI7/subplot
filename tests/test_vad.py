#!/usr/bin/env python3
"""Tests for vad.py: speech spans from probabilities and the cross-correlation timing measurement, written against the
ARCHITECTURE "sync.py" section. Run with the sync venv's interpreter: <VENV>/bin/python tests/test_vad.py
(the measurement needs numpy; without it every test here is skipped, the other test files still run with python3).
No media: speech spans are synthetic; one test decodes 70 s of generated silence through the real model when it loads."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/
import os, random, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
TMPHOME = os.path.realpath(tempfile.mkdtemp(prefix="subs-vad-", dir=os.environ.get("SUBS_SANDBOX")))
os.environ["SUBS_HOME"] = TMPHOME
os.environ["SUBS_STATE"] = os.path.join(TMPHOME, "state")
os.makedirs(os.environ["SUBS_STATE"], mode=0o700, exist_ok=True)

import vad  # noqa: E402

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


def _talk(seed=1, duration_ms=1500000, n=320):
    """Synthetic dialogue: n speech spans of 0.8-4 s with 0.3-6 s gaps across duration_ms."""
    rnd = random.Random(seed)
    spans, t = [], 2000
    while len(spans) < n and t < duration_ms - 10000:
        length = rnd.randint(800, 4000)
        spans.append((t, t + length))
        t += length + rnd.randint(300, 6000)
    return spans


def _cues(spans, shift=0, every=1):
    return [(a + shift, b + shift, f"line {i}") for i, (a, b) in enumerate(spans) if i % every == 0]


@unittest.skipUnless(HAVE_NUMPY, "numpy missing: run with the subs venv")
class MeasureTests(unittest.TestCase):
    DUR = 1500000

    def test_spans_from_probs_merges_pads_and_drops_short(self):
        probs = [0.0] * 10 + [0.9] * 20 + [0.0] * 5 + [0.9] * 20 + [0.0] * 50 + [0.9] * 3 + [0.0] * 10
        spans = vad.spans_from_probs(probs)
        self.assertEqual(len(spans), 1, spans)                       # the 160 ms gap is merged, the 96 ms blip dropped
        a, b = spans[0]
        self.assertEqual(a, int(10 * vad.FRAME_MS - vad.PAD_MS))
        self.assertEqual(b, int(55 * vad.FRAME_MS + vad.PAD_MS))
        self.assertEqual(vad.spans_from_probs([]), [])
        self.assertEqual(vad.spans_from_probs([0.2] * 100), [])

    def test_in_sync_cues_measure_near_zero(self):
        spans = _talk()
        m = vad.measure(_cues(spans), spans, self.DUR)
        self.assertEqual(m["why"], "")
        self.assertLessEqual(abs(m["offset_ms"]), 100, m)
        self.assertTrue(all(abs(h) <= 100 for h in m["halves"]), m)

    def test_planted_shifts_are_recovered_with_the_right_sign(self):
        spans = _talk(seed=2)
        for planted in (3000, -20000, 59000, -1700):
            m = vad.measure(_cues(spans, shift=planted), spans, self.DUR)
            self.assertIsNotNone(m["offset_ms"], (planted, m))
            # the cues sit `planted` ms late, so they must move by -planted to meet the voice
            self.assertLessEqual(abs(m["offset_ms"] + planted), 150, (planted, m))

    def test_sparse_subtitle_still_measures(self):
        spans = _talk(seed=3)
        m = vad.measure(_cues(spans, shift=12000, every=3), spans, self.DUR)
        self.assertLessEqual(abs(m["offset_ms"] + 12000), 150, m)

    def test_halves_that_disagree_give_no_offset(self):
        spans = _talk(seed=4)
        mid = self.DUR / 2
        cues = [(a + (0 if a < mid else 9000), b + (0 if a < mid else 9000), "x") for a, b in spans]   # another cut: an ad break
        m = vad.measure(cues, spans, self.DUR)
        self.assertIsNone(m["offset_ms"])
        self.assertEqual(m["why"], "halves disagree")
        self.assertLessEqual(abs(m["halves"][0]), 150, m)
        self.assertLessEqual(abs(m["halves"][1] + 9000), 150, m)

    def test_too_few_cues_or_nothing_gives_no_offset(self):
        spans = _talk(seed=5)
        m = vad.measure(_cues(spans)[:vad.MIN_CUES_PER_HALF + 2], spans, self.DUR)       # all in the first half
        self.assertEqual((m["offset_ms"], m["why"]), (None, "too few cues"))
        self.assertEqual(vad.measure([], spans, self.DUR)["offset_ms"], None)
        self.assertEqual(vad.measure(_cues(spans), [], self.DUR)["offset_ms"], None)
        self.assertEqual(vad.measure(_cues(spans), spans, 0)["offset_ms"], None)

    def test_regions_two_windows_for_a_long_track_and_halves_for_a_short_one(self):
        self.assertEqual(vad.regions_for(600000), [(0, 300000), (300000, 600000)])
        self.assertEqual(vad.regions_for(0), [(0, 0), (0, 0)])
        long = vad.regions_for(7200000)
        self.assertEqual(long, [(1620000, 1980000), (5220000, 5580000)])          # 6 min centred at 25 % and 75 %
        spans = _talk(seed=8, duration_ms=7200000, n=2000)
        m = vad.measure(_cues(spans, shift=-15000), spans, 7200000, long)          # only the windows' speech is known
        inside = [(a, b) for a, b in spans if any(lo <= a < hi for lo, hi in long)]
        m2 = vad.measure(_cues(spans, shift=-15000), inside, 7200000, long)
        for r in (m, m2):
            self.assertLessEqual(abs(r["offset_ms"] - 15000), 150, r)

    def test_offset_beyond_the_search_range_is_not_a_measurement(self):
        spans = _talk(seed=6)
        m = vad.measure(_cues(spans, shift=vad.SEARCH_MS + 30000), spans, self.DUR)
        self.assertIsNone(m["offset_ms"], m)
        self.assertIn(m["why"], ("weak peak", "no peak", "halves disagree"), m)   # two windows agreed by chance once: MIN_Z catches it


class LoadTests(unittest.TestCase):
    def test_wrong_model_hash_is_unavailable(self):
        saved = vad.MODEL_SHA256, vad._session
        vad.MODEL_SHA256, vad._session = "0" * 64, None
        try:
            with self.assertRaises(vad.Unavailable):
                vad.load()
        finally:
            vad.MODEL_SHA256, vad._session = saved

    def test_missing_model_is_unavailable(self):
        saved = vad.MODEL, vad._session
        vad.MODEL, vad._session = os.path.join(TMPHOME, "nope.onnx"), None
        try:
            with self.assertRaises(vad.Unavailable):
                vad.load()
        finally:
            vad.MODEL, vad._session = saved

    def test_silence_decodes_to_no_speech_and_ffmpeg_failures_are_speech_errors(self):
        try:
            vad.load()
        except vad.Unavailable as exc:
            self.skipTest(f"detector unavailable here: {exc}")
        wav = os.path.join(TMPHOME, "silence.wav")
        subprocess.run([vad.C.FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                        "-t", "70", wav], check=True, timeout=120)
        spans, duration_ms, regions = vad.speech_spans(wav)
        self.assertEqual(spans, [])
        self.assertTrue(69000 <= duration_ms <= 71000, duration_ms)
        self.assertEqual(regions, vad.regions_for(duration_ms))
        with self.assertRaises(vad.SpeechError):                                  # Plex says 2 h, the file holds 70 s: a window is empty
            vad.speech_spans(wav, 7200000)
        with self.assertRaises(vad.SpeechError) as ctx:
            vad.speech_spans(os.path.join(TMPHOME, "missing.mkv"))
        self.assertIn("ffmpeg exit", str(ctx.exception))
        short = os.path.join(TMPHOME, "short.wav")
        subprocess.run([vad.C.FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                        "-t", "5", short], check=True, timeout=120)
        with self.assertRaises(vad.SpeechError):
            vad.speech_spans(short)


if __name__ == "__main__":
    unittest.main()
