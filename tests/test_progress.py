"""Unit tests for the progress feature: current.json, current_payload and read_progress.

Run with:  python3 test_progress.py
No PC, no network: read_progress is driven by a fake runner.
Written from the "Tests" section of the Progress spec; the module sources are not read.
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/

import datetime
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TMPHOME = os.path.realpath(tempfile.mkdtemp(prefix="subs-progress-"))
os.environ["SUBS_HOME"] = TMPHOME
os.environ["SUBS_STATE"] = os.path.join(TMPHOME, "state")
os.environ["SUBS_RULES"] = os.path.join(TMPHOME, "rules.json")
os.environ["SUBS_FFMPEG"] = "/bin/false"
os.environ["SUBS_FFPROBE"] = "/bin/false"
os.makedirs(os.environ["SUBS_STATE"], mode=0o700, exist_ok=True)

import common as C  # noqa: E402
import gen  # noqa: E402
import server  # noqa: E402

RULES = {"exclude_libraries": ["Hentai"]}


def iso(offset_s=0):
    """UTC timestamp in the format the writers use, shifted by offset_s seconds."""
    return (datetime.datetime.utcnow() + datetime.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def base(**fields):
    d = {"at": iso(), "step": "gen", "key": "tv-1-2", "show": "Some Show", "title": "Episode Title",
         "season": 1, "episode": 2, "library": "TV", "lang": "en", "phase": "transcribing",
         "n": 1, "of": 3, "done_ms": 1000, "of_ms": 2000, "started_at": iso(-60)}
    d.update(fields)
    return d


def write_current(**fields):
    data = base(**fields)
    C.write_json(C.CURRENT, data)
    return data


class Runner:
    """A fake run(args, timeout) that records its calls."""

    def __init__(self, payload, rc=0):
        self.payload = payload
        self.rc = rc
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((args, timeout))
        return self.rc, self.payload, ""


def runner(payload, rc=0):
    return Runner(payload, rc)


def command_text(run):
    args = run.calls[0][0]
    if isinstance(args, (list, tuple)):
        return " ".join(str(a) for a in args)
    return str(args)


class CurrentTests(unittest.TestCase):
    def test_set_current_merges_and_stamps_at(self):
        C.set_current(step="gen", key="tv-1-2", title="Episode Title", phase="extracting")
        first = C.read_json(C.CURRENT, 4096)
        self.assertEqual((first["step"], first["key"], first["title"], first["phase"]),
                         ("gen", "tv-1-2", "Episode Title", "extracting"))
        self.assertIsInstance(first["at"], str)
        datetime.datetime.strptime(first["at"], "%Y-%m-%dT%H:%M:%SZ")
        C.set_current(phase="uploading", n=2)
        second = C.read_json(C.CURRENT, 4096)
        self.assertEqual((second["phase"], second["n"]), ("uploading", 2))
        self.assertEqual((second["step"], second["key"], second["title"]),
                         ("gen", "tv-1-2", "Episode Title"))
        self.assertIsInstance(second["at"], str)

    def test_clear_current_removes_file_and_is_quiet_when_absent(self):
        C.set_current(step="find", key="tv-1-1")
        self.assertTrue(os.path.exists(C.CURRENT))
        C.clear_current()
        self.assertFalse(os.path.exists(C.CURRENT))
        C.clear_current()
        self.assertFalse(os.path.exists(C.CURRENT))


class CurrentPayloadTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(C.CURRENT):
            os.unlink(C.CURRENT)

    def test_null_when_absent(self):
        self.assertIsNone(server.current_payload(RULES))

    def test_null_when_at_is_twenty_minutes_old(self):
        C.write_json(C.CURRENT, base(at=iso(-20 * 60)))
        self.assertIsNone(server.current_payload(RULES))

    def test_unknown_fields_and_paths_are_dropped(self):
        C.write_json(C.CURRENT, base(path="C:\\Subplot\\secret.mkv", plex_path="/media/secret.mkv", bogus="x"))
        payload = server.current_payload(RULES)
        self.assertNotIn("path", payload)
        self.assertNotIn("plex_path", payload)
        self.assertNotIn("bogus", payload)

    def test_github_token_in_title_becomes_secret(self):
        token = "ghp_" + "A" * 36
        C.write_json(C.CURRENT, base(title=token))
        payload = server.current_payload(RULES)
        self.assertNotIn(token, payload["title"])
        self.assertIn("[secret]", payload["title"])

    def test_invalid_numbers_are_dropped_and_valid_ints_stay(self):
        C.write_json(C.CURRENT, base(n=-1, of=float("nan"), done_ms=10 ** 11, season=True, episode=5, of_ms=1234))
        payload = server.current_payload(RULES)
        self.assertNotIn("n", payload)
        self.assertNotIn("of", payload)
        self.assertNotIn("done_ms", payload)
        self.assertNotIn("season", payload)
        self.assertEqual(payload["episode"], 5)
        self.assertEqual(payload["of_ms"], 1234)

    def test_null_for_excluded_library(self):
        C.write_json(C.CURRENT, base(library="Hentai"))
        self.assertIsNone(server.current_payload(RULES))

    def test_null_for_unknown_step(self):
        C.write_json(C.CURRENT, base(step="place"))
        self.assertIsNone(server.current_payload(RULES))


class ReadProgressTests(unittest.TestCase):
    def test_reads_done_ms(self):
        self.assertEqual(gen.read_progress("C:/Subplot/jobs/ep-1", runner(b'{"done_ms": 1234}')), 1234)

    def test_none_when_runner_fails(self):
        self.assertIsNone(gen.read_progress("C:/Subplot/jobs/ep-1", runner(b'{"done_ms": 1234}', rc=1)))

    def test_none_for_bad_done_ms(self):
        for body in (b'{"done_ms": -1}', b'{"done_ms": 1000000000000}', b'{"done_ms": "x"}', b'{"done_ms": true}'):
            with self.subTest(body=body):
                self.assertIsNone(gen.read_progress("C:/Subplot/jobs/ep-1", runner(body)))

    def test_none_for_oversized_body(self):
        body = b'{"done_ms": 1234, "pad": "' + b"a" * 5000 + b'"}'
        self.assertIsNone(gen.read_progress("C:/Subplot/jobs/ep-1", runner(body)))

    def test_command_names_progress_json_under_remote(self):
        remote = "C:/Subplot/jobs/ep-1"
        run = runner(b'{"done_ms": 1234}')
        gen.read_progress(remote, run)
        cmd = command_text(run)
        self.assertIn("progress.json", cmd)
        self.assertIn(remote, cmd)


if __name__ == "__main__":
    unittest.main()
