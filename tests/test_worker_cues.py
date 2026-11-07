#!/usr/bin/env python3
"""Unit tests for subs_worker.build_cues, written from ARCHITECTURE "PC side".

Run with:  python3 pc/test_worker_cues.py

Standard library only. No model, no network, no audio, no job folder.
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subs_worker


MAX_CUE_MS = 7000
MAX_CHARS = 42
MAX_LINES = 2
MAX_GAP_MS = 1500

DEFAULTS = {
    "max_cue_ms": MAX_CUE_MS,
    "max_chars": MAX_CHARS,
    "max_lines": MAX_LINES,
    "max_gap_ms": MAX_GAP_MS,
}


class Word:
    """A faster-whisper-like word that supports attribute AND dict access."""

    __slots__ = ("start", "end", "word")

    def __init__(self, start, end, word):
        self.start = start
        self.end = end
        self.word = word

    def __getitem__(self, key):
        if key not in self.__slots__:
            raise KeyError(key)
        return getattr(self, key)

    def __repr__(self):
        return "Word(%r, %r, %r)" % (self.start, self.end, self.word)


def words(*specs):
    """Build a word list from (start_seconds, end_seconds, text) tuples."""
    return [Word(start, end, text) for start, end, text in specs]


def build(word_list, **overrides):
    kwargs = dict(DEFAULTS)
    kwargs.update(overrides)
    return subs_worker.build_cues(word_list, **kwargs)


def norm(text):
    """Collapse all whitespace so wrapping/joining details don't matter."""
    return " ".join(text.split())


class BuildCuesTests(unittest.TestCase):
    def assertMs(self, actual, expected, delta=2):
        self.assertAlmostEqual(actual, expected, delta=delta)

    def assertCuesOK(self, cues, max_cue_ms=MAX_CUE_MS,
                     max_chars=MAX_CHARS, max_lines=MAX_LINES):
        self.assertIsInstance(cues, list)
        previous = None
        for start, end, text in cues:
            self.assertIsInstance(start, (int, float))
            self.assertIsInstance(end, (int, float))
            self.assertIsInstance(text, str)
            self.assertGreaterEqual(end - start, 200)
            self.assertLessEqual(end - start, max_cue_ms + 1)
            self.assertTrue(text.strip())
            lines = text.splitlines()
            self.assertLessEqual(len(lines), max_lines)
            for line in lines:
                self.assertLessEqual(len(line), max_chars)
            if previous is not None:
                prev_start, prev_end, _ = previous
                self.assertLessEqual(prev_start, start)
                self.assertLessEqual(prev_end, start + 1)
            previous = (start, end, text)

    def test_empty_list(self):
        self.assertEqual(build([]), [])

    def test_whitespace_only_words_produce_no_cue(self):
        ws = words(
            (0.0, 0.5, " "),
            (0.5, 1.0, "   "),
            (1.0, 1.5, "\t"),
            (1.5, 2.0, "\n"),
        )
        self.assertEqual(build(ws), [])

    def test_sentence_end_closes_cue_after_one_second(self):
        ws = words(
            (0.0, 0.4, " Hello"),
            (0.4, 1.2, " there."),
            (1.2, 1.7, " Next"),
        )
        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 2)
        self.assertMs(cues[0][0], 0)
        self.assertMs(cues[0][1], 1200)
        self.assertEqual(norm(cues[0][2]), "Hello there.")
        self.assertMs(cues[1][0], 1200)
        self.assertMs(cues[1][1], 1700)
        self.assertEqual(norm(cues[1][2]), "Next")

    def test_sentence_end_before_one_second_does_not_close(self):
        ws = words(
            (0.0, 0.5, " Hi."),
            (0.5, 0.9, " there"),
            (0.9, 1.4, " friend"),
        )
        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 1)
        self.assertMs(cues[0][0], 0)
        self.assertMs(cues[0][1], 1400)
        self.assertEqual(norm(cues[0][2]), "Hi. there friend")

    def test_exclamation_and_question_close_after_one_second(self):
        ws = words(
            (0.0, 1.2, " Wow!"),
            (1.2, 1.6, " Next"),
            (1.6, 2.8, " Yes?"),
            (2.8, 3.2, " End"),
        )
        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 3)
        self.assertEqual([norm(c[2]) for c in cues],
                         ["Wow!", "Next Yes?", "End"])
        self.assertMs(cues[0][1], 1200)
        self.assertMs(cues[1][1], 2800)
        self.assertMs(cues[2][0], 2800)

    def test_long_word_is_clipped_to_max_cue_ms(self):
        cues = build(words((0.0, 70.0, " Supercalifragilistic")))
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 1)
        self.assertMs(cues[0][0], 0)
        self.assertMs(cues[0][1], MAX_CUE_MS)

    def test_gap_over_max_gap_splits(self):
        ws = words(
            (0.0, 1.0, " Hello"),
            (2.6, 3.2, " world"),
        )
        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 2)
        self.assertMs(cues[0][0], 0)
        self.assertMs(cues[0][1], 1000)
        self.assertMs(cues[1][0], 2600)
        self.assertMs(cues[1][1], 3200)
        self.assertEqual(norm(cues[0][2]), "Hello")
        self.assertEqual(norm(cues[1][2]), "world")

    def test_gap_within_max_does_not_split(self):
        ws = words(
            (0.0, 1.0, " Hi"),
            (2.0, 2.5, " there"),
        )
        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 1)
        self.assertMs(cues[0][0], 0)
        self.assertMs(cues[0][1], 2500)
        self.assertEqual(norm(cues[0][2]), "Hi there")

    def test_wrapping_limits_and_long_run_splits_over_same_span(self):
        specs = []
        for i in range(40):
            specs.append((i * 0.5, i * 0.5 + 0.45, " word"))
        ws = words(*specs)
        last_end_ms = (39 * 0.5 + 0.45) * 1000

        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertGreaterEqual(len(cues), 2)

        self.assertMs(cues[0][0], 0)
        self.assertMs(cues[-1][1], last_end_ms)

        joined = norm(" ".join(norm(cue[2]) for cue in cues))
        expected = norm(" ".join(norm(w.word) for w in ws))
        self.assertEqual(joined, expected)

        for _, _, text in cues:
            lines = text.splitlines()
            self.assertLessEqual(len(lines), MAX_LINES)
            for line in lines:
                self.assertLessEqual(len(line), MAX_CHARS)

    def test_cue_end_capped_at_next_cue_start(self):
        ws = words(
            (0.0, 2.0, " Hello."),
            (1.5, 2.5, " world"),
        )
        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 2)
        self.assertMs(cues[0][1], 1500)
        self.assertMs(cues[1][0], 1500)

    def test_cue_is_at_least_200_ms_long(self):
        cues = build(words((3.0, 3.0, " Hi")))
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 1)
        self.assertMs(cues[0][0], 3000)
        self.assertGreaterEqual(cues[0][1] - cues[0][0], 200)
        self.assertLessEqual(cues[0][1], 3000 + MAX_CUE_MS)

    def test_mixed_sequence_is_monotonic_and_bounded(self):
        ws = words(
            (0.0, 0.4, " Hello"),
            (0.4, 1.4, " world."),
            (1.4, 1.9, " This"),
            (1.9, 2.4, " is"),
            (2.4, 3.0, " a"),
            (3.0, 3.6, " test."),
            (5.5, 6.0, " Again"),
        )
        cues = build(ws)
        self.assertCuesOK(cues)
        self.assertEqual(len(cues), 3)
        self.assertMs(cues[0][0], 0)
        self.assertMs(cues[0][1], 1400)
        self.assertMs(cues[1][0], 1400)
        self.assertMs(cues[1][1], 3600)
        self.assertMs(cues[2][0], 5500)
        self.assertMs(cues[2][1], 6000)
        self.assertEqual(
            norm(" ".join(norm(c[2]) for c in cues)),
            norm("Hello world. This is a test. Again"),
        )


if __name__ == "__main__":
    unittest.main()
