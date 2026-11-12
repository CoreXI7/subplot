"""Unit tests for gen.py: extraction, the picker and the yielding watcher.

Run with:  python3 test_gen.py
No PC, no ffmpeg, no media: the runner and the probe are fake.
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TMPHOME = os.path.realpath(tempfile.mkdtemp(prefix="subs-gen-"))
os.environ["SUBS_HOME"] = TMPHOME
os.environ["SUBS_STATE"] = os.path.join(TMPHOME, "state")
os.environ["SUBS_RULES"] = os.path.join(TMPHOME, "rules.json")
os.environ["SUBS_FFMPEG"] = "/bin/false"
os.environ["SUBS_FFPROBE"] = "/bin/false"
os.makedirs(os.environ["SUBS_STATE"], mode=0o700, exist_ok=True)

import common as C  # noqa: E402
import gen  # noqa: E402


def fake_run_factory(frames_ok):
    """A runner that answers ffprobe with 20 minutes, writes the audio file, and writes frames only when frames_ok."""
    def run(args, timeout):
        if "-show_entries" in args:                       # ffprobe (both stubs point at /bin/false, so look at the args)
            return 0, b"1200.0\n", ""
        if args[-1].endswith(".opus"):
            with open(args[-1], "wb") as fh:
                fh.write(b"opus" * 100)
            return 0, b"", ""
        if args[-1].endswith(".jpg"):
            if not frames_ok:
                return 1, b"", "no frame"
            with open(args[-1], "wb") as fh:
                fh.write(b"\xff\xd8jpeg")
            return 0, b"", ""
        return 0, b"", ""
    return run


class FakeProc:
    """A subprocess that never finishes on its own."""
    def __init__(self, *a, **k):
        self.killed = False

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("ssh", timeout)

    def kill(self):
        self.killed = True


class GenTests(unittest.TestCase):
    def test_probe_script_exists(self):
        """The PC probe is read from the repo's pc/ folder; a wrong path means 'no probe' forever and no generation."""
        self.assertTrue(os.path.isfile(gen.PROBE), gen.PROBE)

    def test_outside_hours_never_contacts_the_pc(self):
        """Back-to-back runs start every minute: outside the idle hours gen.main must not SSH to the PC at all."""
        rules = {"pc_idle": {"hours": ["mon-sun 03:00-03:01"]}, "generate": {"max_hours_per_night": 6}}
        calls = []
        with mock.patch.object(C, "load_rules", return_value=rules), mock.patch.object(C, "pc_override", return_value=None), \
                mock.patch.object(C, "inside_idle_hours", return_value=False), \
                mock.patch.object(gen, "run", side_effect=lambda *a, **k: calls.append(a) or (1, b"", b"")):
            self.assertEqual(gen.main(), 0)
        self.assertEqual(calls, [])
        self.assertEqual((C.read_json(C.PC_STATUS, 8192) or {}).get("why"), "outside the idle hours")

    def test_inside_idle_hours(self):
        import datetime
        r = {"pc_idle": {"hours": ["mon-fri 09:00-17:00", "sat 23:00-02:00"]}}
        mon, sat, sun = datetime.datetime(2026, 11, 9, 10, 0), datetime.datetime(2026, 11, 7, 23, 30), datetime.datetime(2026, 11, 8, 1, 30)
        self.assertTrue(C.inside_idle_hours(r, mon))
        self.assertFalse(C.inside_idle_hours(r, mon.replace(hour=17)), "the end is exclusive")
        self.assertTrue(C.inside_idle_hours(r, sat), "a span past midnight starts on its own day")
        self.assertTrue(C.inside_idle_hours(r, sun), "and runs into the next morning")
        self.assertFalse(C.inside_idle_hours(r, sun.replace(hour=3)))

    def test_done_keeps_timings_only_when_given(self):
        C.done({"step": "gen", "key": "ep-1", "result": "queued", "extract_s": 12, "pc_s": 95})
        C.done({"step": "gen", "key": "ep-2", "result": "failed", "extract_s": "x", "pc_s": True})
        import json as _j
        rows = [_j.loads(l) for l in open(C.DONE_LOG)][-2:]
        self.assertEqual((rows[0]["extract_s"], rows[0]["pc_s"]), (12, 95))
        self.assertNotIn("extract_s", rows[1]); self.assertNotIn("pc_s", rows[1])

    def test_extract_needs_frames(self):
        out = os.path.join(TMPHOME, "job-noframes")
        ok, why, duration = gen.extract("/nonexistent/video.mkv", out, {}, fake_run_factory(frames_ok=False))
        self.assertEqual((ok, duration), (False, 1200000))
        self.assertIn("frames failed", why)
        out = os.path.join(TMPHOME, "job-frames")
        ok, why, duration = gen.extract("/nonexistent/video.mkv", out, {}, fake_run_factory(frames_ok=True))
        self.assertEqual((ok, why), (True, ""))
        self.assertEqual(len(os.listdir(os.path.join(out, "frames"))), gen.FRAMES)

    def test_extract_takes_the_english_audio_track(self):
        """Dual-audio anime puts Japanese first: the English track (audio index 1 here) is the one sent to Whisper."""
        out = tempfile.mkdtemp(dir=self.tmp) if hasattr(self, "tmp") else tempfile.mkdtemp()
        base = fake_run_factory(True)
        seen = []
        def run(args, timeout):
            if "stream_tags=language" in args:
                return 0, b"jpn\neng\n", ""
            if args[-1].endswith(".opus"):
                seen.append(args)
            return base(args, timeout)
        ok, why, _ = gen.extract("/media/x.mkv", out, {}, run)
        self.assertTrue(ok, why)
        self.assertIn("0:a:1", seen[0])
        self.assertEqual(gen.english_audio_index("/media/x.mkv", lambda a, t: (0, b"spa\n", "")), 0)

    def test_pick_item_skips_items_with_region_tagged_english_subs(self):
        item = {"status": "ok", "library": "Anime", "need": ["en"], "views": {"alice": 3}, "added_at": 1,
                "audio": [{"lang": "ja-jp"}, {"lang": "en-us"}], "subs": [{"lang": "en-us", "kind": "text"}]}
        self.assertIsNone(gen.pick_item({"items": {"ep-1": item}}, {}, {}, {}, set(), []))
        item["subs"] = []
        self.assertEqual(gen.pick_item({"items": {"ep-1": item}}, {}, {}, {}, set(), [])[0], "ep-1")

    def test_watcher_yields_to_rules_process(self):
        """The yield_to list from rules reaches the watcher: a configured process working the GPU preempts the worker."""
        probes = iter([{"gpu_util": 90, "busy_procs": ["lmstudio.exe"], "processes": [], "game_procs": []}])
        rules = {"pc_idle": {"yield_to": ["lmstudio.exe"]}}
        calls = []
        def run(args, timeout):
            calls.append(args[-1]); return 0, b"", ""
        with mock.patch("subprocess.Popen", FakeProc):
            rc, why = gen.run_worker_watched("C:\\Subplot\\jobs\\ep-1", run, rules, probe=lambda r: next(probes), poll_s=0)
        self.assertEqual((rc, why), (1, "preempted: lmstudio.exe"))
        self.assertTrue(any("Stop-Process" in c for c in calls), "the remote worker must be killed")

    def test_watcher_ignores_our_own_python(self):
        probes = iter([{"gpu_util": 90, "busy_procs": ["python.exe"], "processes": [], "game_procs": []},
                       {"gpu_util": 90, "busy_procs": ["ollama.exe"], "processes": [], "game_procs": []}])
        with mock.patch("subprocess.Popen", FakeProc):
            rc, why = gen.run_worker_watched("C:\\Subplot\\jobs\\ep-1", lambda a, t: (0, b"", ""), None, probe=lambda r: next(probes), poll_s=0)
        self.assertEqual((rc, why), (1, "preempted: ollama.exe"))

    def test_pick_item_skips_busy_burned_excluded(self):
        def item(k, lib="TV"):
            return {"status": "ok", "library": lib, "need": ["en"], "views": {"alice": 3}, "added_at": 1,
                    "audio": [{"lang": "en"}], "subs": [], "show_plex_id": 1}
        library = {"items": {"ep-1": item("ep-1"), "ep-2": item("ep-2"), "ep-3": item("ep-3"), "ep-4": item("ep-4"), "ep-5": item("ep-5")}}
        rules = {"priority": {"libraries": []}, "exclude_libraries": ["Hentai"]}
        marks_all = {"ep-2": {"burned_in": True}, "ep-3": {"gen_failed": 2}}
        decisions_all = {"ep-4": {"decision": "exclude"}}
        picked = gen.pick_item(library, rules, marks_all, decisions_all, {"ep-1.en"}, [])
        self.assertEqual(picked[:2], ("ep-5", "en"))
        library["items"]["ep-5"]["library"] = "Hentai"
        self.assertIsNone(gen.pick_item(library, rules, marks_all, decisions_all, {"ep-1.en"}, []))
        # audio language: untagged audio is fine (Whisper detects it), a track tagged another language is not
        library["items"]["ep-5"]["library"] = "TV"
        library["items"]["ep-5"]["audio"] = [{"lang": None}]
        self.assertEqual(gen.pick_item(library, rules, marks_all, decisions_all, {"ep-1.en"}, [])[:2], ("ep-5", "en"))
        library["items"]["ep-5"]["audio"] = [{"lang": "ja"}]
        self.assertIsNone(gen.pick_item(library, rules, marks_all, decisions_all, {"ep-1.en"}, []))
        library["items"]["ep-5"]["audio"] = [{"lang": "ja"}, {"lang": "eng"}]
        self.assertEqual(gen.pick_item(library, rules, marks_all, decisions_all, {"ep-1.en"}, [])[:2], ("ep-5", "en"))

    def test_pick_item_skips_items_we_already_placed(self):
        # library.json comes from the nightly Plex copy, so an item placed today still lists 'en' as needed: the placed mark
        # (with its file still on disk) must stop a regeneration; a request from the owner still wins
        import tempfile
        def item(k):
            return {"status": "ok", "library": "TV", "need": ["en"], "views": {"alice": 3}, "added_at": 1,
                    "audio": [{"lang": "en"}], "subs": [], "show_plex_id": 1}
        library = {"items": {"ep-1": item("ep-1"), "ep-2": item("ep-2")}}
        rules = {"priority": {"libraries": []}, "exclude_libraries": ["Hentai"]}
        with tempfile.TemporaryDirectory(dir=os.environ.get("SUBS_SANDBOX")) as d:
            sidecar = os.path.join(d, "ep-1.en.srt")
            with open(sidecar, "w") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
            marks_all = {"ep-1": {"placed": {"en": {"path": sidecar, "pending": False}}}}
            self.assertEqual(gen.pick_item(library, rules, marks_all, {}, set(), [])[:2], ("ep-2", "en"))
            library["items"]["ep-2"]["views"] = {}
            library["items"]["ep-1"]["views"] = {"alice": 9}                      # ep-1 would rank first without the mark
            self.assertEqual(gen.pick_item(library, rules, marks_all, {}, set(), [])[:2], ("ep-2", "en"))
            # an explicit regenerate request for ep-1 is still honoured
            self.assertEqual(gen.pick_item(library, rules, marks_all, {}, set(), [("r1", {"kind": "generate", "key": "ep-1"})])[:2], ("ep-1", "en"))
            # a pending record or a deleted file does not count as placed
            marks_all["ep-1"]["placed"]["en"]["pending"] = True
            self.assertEqual(gen.pick_item(library, rules, marks_all, {}, set(), [])[:2], ("ep-1", "en"))
            marks_all["ep-1"]["placed"]["en"]["pending"] = False
            os.unlink(sidecar)
            self.assertEqual(gen.pick_item(library, rules, marks_all, {}, set(), [])[:2], ("ep-1", "en"))

    def test_unload_idle_model_only_when_vram_is_the_sole_block(self):
        """The owner's choice: Ollama's idle model is unloaded only inside the hours, only after after_min, only when nothing else
        blocks, only via the configured LAN URL, and only with validated model names."""
        import datetime
        calls = []
        def http(method, url, body=None):
            calls.append((method, url, body))
            if url.endswith("/api/ps"):
                return b'{"models":[{"name":"llama3:8b"},{"name":"bad name;rm"},{"name":7}]}'
            return b'{"done":true}'
        rules = {"pc_idle": {"hours": ["00:00-23:59"], "no_processes": ["game.exe"], "gpu_util_below": 20, "vram_free_mb": 6000,
                             "unload_idle_model": {"ollama": "http://10.0.0.50:11434", "after_min": 10}}}
        held = {"gpu_util": 2, "gpu_mem_used_mb": 31000, "gpu_mem_total_mb": 32600, "busy_procs": [], "game_procs": [], "processes": ["ollama.exe", "explorer.exe"]}
        old = (datetime.datetime.utcnow() - datetime.timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # first sighting: only records when the block started
        status = {}
        self.assertEqual(gen.unload_idle_model(rules, held, {}, status, http), [])
        self.assertIn("vram_blocked_since", status); self.assertEqual(calls, [])
        # a game, a busy GPU, no Ollama process, or VRAM held by another program: never
        for bad in (dict(held, processes=["ollama.exe", "game.exe"]), dict(held, gpu_util=50), dict(held, processes=["lmstudio.exe"]),
                    dict(held, busy_procs=["ollama.exe"])):
            self.assertEqual(gen.unload_idle_model(rules, bad, {"vram_blocked_since": old}, {}, http), [])
        self.assertEqual(calls, [])
        # without the rule, or with enough VRAM already: nothing to do
        self.assertEqual(gen.unload_idle_model({"pc_idle": dict(rules["pc_idle"], unload_idle_model=None)}, held, {"vram_blocked_since": old}, {}, http), [])
        self.assertEqual(gen.unload_idle_model(rules, dict(held, gpu_mem_used_mb=100), {"vram_blocked_since": old}, {}, http), [])
        self.assertEqual(calls, [])
        # blocked long enough by an idle Ollama model: unload the valid names only, via the configured URL only
        status = {}
        self.assertEqual(gen.unload_idle_model(rules, held, {"vram_blocked_since": old}, status, http), ["llama3:8b"])
        self.assertEqual(status["unloaded"], ["llama3:8b"])
        self.assertEqual(calls, [("GET", "http://10.0.0.50:11434/api/ps", None),
                                 ("POST", "http://10.0.0.50:11434/api/generate", {"model": "llama3:8b", "keep_alive": 0})])
        # garbage from the PC is ignored
        self.assertEqual(gen.ollama_loaded("http://10.0.0.50:11434", lambda m, u, b=None: b"not json"), [])
        self.assertEqual(gen.ollama_loaded("http://10.0.0.50:11434", lambda m, u, b=None: None), [])
        # the rules validator pins the URL to plain http on the LAN (checked on the example rules, media roots in temp folders)
        import json, tempfile
        real = json.load(open(os.path.join(_ROOT, "config", "rules.example.json")))
        roots = [tempfile.mkdtemp(dir=os.environ.get("SUBS_SANDBOX") or None) for _ in real["media_roots"]]
        real["path_map"] = {k: v.replace(old, new, 1) for k, v in real["path_map"].items() for old, new in zip(real["media_roots"], roots) if v.startswith(old)}
        real["media_roots"] = roots
        real = C.fill_defaults(real)                                   # the example holds only paths; the rest is defaults
        self.assertIsNone(C._rules_problem(json.loads(json.dumps(real))), "the example rules must validate")
        real["pc_idle"]["unload_idle_model"] = {"ollama": "http://10.0.0.50:11434", "after_min": 10}
        self.assertIsNone(C._rules_problem(json.loads(json.dumps(real))), "the README's unload block must validate")
        if True:
            for bad in ("https://10.0.0.50:11434", "http://1.2.3.4:11434", "http://10.0.0.50:11434/x", 5):
                r = json.loads(json.dumps(real)); r["pc_idle"]["unload_idle_model"] = {"ollama": bad}
                self.assertIn("unload_idle_model", C._rules_problem(r) or "", bad)
            r = json.loads(json.dumps(real)); r["pc_idle"]["unload_idle_model"]["after_min"] = -1
            self.assertIn("after_min", C._rules_problem(r) or "")

    def test_pc_paused_states(self):
        """The Pause file fails closed: missing is 'not paused', a valid object and garbage bytes both pause."""
        path = C.PC_PAUSE
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.unlink(path)
            self.assertFalse(C.pc_paused())
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "w") as fh:
                fh.write('{"at": "2026-11-12T08:52:54Z", "by": "page"}')
            self.assertTrue(C.pc_paused())
            with open(path, "wb") as fh:
                fh.write(b"\x00not json{{")
            self.assertTrue(C.pc_paused(), "garbage must fail closed")
        finally:
            if os.path.exists(path) or os.path.islink(path):
                os.unlink(path)

    def test_paused_main_does_nothing(self):
        """While paused main must not probe, SSH or run: it only records why 'paused' and exits 0."""
        rules = {"pc_idle": {"hours": ["mon-sun 00:00-23:59"]}, "generate": {"max_hours_per_night": 6}}
        path = C.PC_PAUSE
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "w") as fh:
                fh.write('{"at": "2026-11-12T08:52:54Z", "by": "page"}')
            run = mock.Mock(return_value=(0, b"", b""))
            probe = mock.Mock(return_value={"gpu_util": 5, "busy_procs": [], "processes": [], "game_procs": []})
            with mock.patch.object(C, "load_rules", return_value=rules), mock.patch.object(gen, "run", run), \
                    mock.patch.object(gen, "probe_pc", probe, create=True):
                self.assertEqual(gen.main(), 0)
            self.assertFalse(run.called, "a paused run must not touch the PC")
            self.assertFalse(probe.called, "a paused run must not probe the PC")
            self.assertEqual(C.read_json(C.PC_STATUS, 8192)["why"], "paused")
        finally:
            if os.path.exists(path) or os.path.islink(path):
                os.unlink(path)

    def test_watcher_stops_on_pause(self):
        """The pause switch kills a running worker: the next poll sees the file and preempts with 'paused'."""
        path = C.PC_PAUSE
        calls = []
        def run(args, timeout):
            calls.append(args[-1])
            return 0, b"", b""
        def probe(rules):
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "w") as fh:
                fh.write('{"at": "2026-11-12T08:52:54Z", "by": "page"}')
            return {"gpu_util": 3, "busy_procs": [], "processes": [], "game_procs": []}
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.unlink(path)
            with mock.patch("subprocess.Popen", FakeProc):
                rc, why = gen.run_worker_watched("C:\\Subplot\\jobs\\ep-1", run, None, probe=probe, poll_s=0)
            self.assertEqual((rc, why), (1, "preempted: paused"))
            self.assertTrue(any("Stop-Process" in c for c in calls), "the worker must be killed")
        finally:
            if os.path.exists(path) or os.path.islink(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
