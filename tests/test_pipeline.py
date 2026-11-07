"""End-to-end-ish tests for the Subs pipeline: scan, sync and place.

Run with:  python3 test_pipeline.py
All state lives in a throw-away temp tree; the external programs are fake.
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/

import inspect
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TMPHOME = os.path.realpath(tempfile.mkdtemp(prefix="subs-pipeline-"))
os.environ["SUBS_HOME"] = TMPHOME
os.environ["SUBS_STATE"] = os.path.join(TMPHOME, "state")
os.environ["SUBS_RULES"] = os.path.join(TMPHOME, "rules.json")
os.environ["SUBS_PLEX_DB"] = os.path.join(TMPHOME, "plex.db.nightly")
os.environ["SUBS_FFMPEG"] = "/bin/false"
os.environ["SUBS_FFPROBE"] = "/bin/false"
os.makedirs(os.environ["SUBS_STATE"], mode=0o700, exist_ok=True)

import common  # noqa: E402
import scan  # noqa: E402
import sync  # noqa: E402
sync.MIN_WINDOW_CUES = 1          # fixtures have two or three cues; production keeps 10 lines inside the clip
import place  # noqa: E402


def _ts(ms):
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _srt(shift_ms=0, text="Hello"):
    return (
        "1\n%s --> %s\n%s\n\n"
        "2\n%s --> %s\nWorld\n"
        % (
            _ts(1000 + shift_ms),
            _ts(2000 + shift_ms),
            text,
            _ts(3000 + shift_ms),
            _ts(4000 + shift_ms),
        )
    )


def _own_srt(key, source="find", confidence=0.9):
    return ("; subplot %s %s %s\n\n" % (key, source, confidence)) + _srt()


class SubsPipelineTest(unittest.TestCase):
    KEY = "ep-1001"
    LANG = "en"

    def setUp(self):
        self.state = common.STATE
        if os.path.isdir(self.state):
            shutil.rmtree(self.state)
        os.makedirs(self.state, mode=0o700)
        self.media = os.path.realpath(tempfile.mkdtemp(prefix="media-", dir=TMPHOME))
        self.ep_video = os.path.join(self.media, "Show", "Season 01", "Show - S01E01.mkv")
        self.movie_video = os.path.join(self.media, "Movie (2020)", "Movie.mkv")
        for path in (self.ep_video, self.movie_video):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"not really a video")
        self.rules = {
            "path_map": {
                "/shows": os.path.join(self.media, "Show"),
                "/movies": os.path.join(self.media, "Movie (2020)"),
            },
            "media_roots": [self.media],
            "exclude_libraries": [],
            "plex": {"machine_id": "0123456789abcdef0123456789abcdef"},
            "languages": {
                "primary": "en",
                "secondary": "es-419",
                "secondary_for_users": ["maria"],
            },
            "thresholds": {"keep": 0.85, "review": 0.60},
            "find": {"daily_budget": 40, "min_score": 70,
                     "sync_max_offset_ms": 1500, "retry_days": 30},
            "generate": {"model": "large-v3", "max_hours_per_night": 6,
                         "burned_in_frames": 12, "burned_in_ratio": 0.5,
                         "es_review_until_kept": 20},
            "pc_idle": {"hours": ["00:00-09:00", "11:00-17:00"],
                        "no_processes": ["steam.exe"], "gpu_util_below": 20},
            "priority": {"watched_days": 30, "libraries": ["Cartoons", "Shows", "Anime"]},
        }
        common.write_json(common.RULES, self.rules)
        self._make_plex_db(common.PLEX_DB_NIGHTLY)
        self.speech_error = None          # a SpeechError message: speech_spans raises it (a non-timing failure -> review)
        self.shift_ms = 0                 # the offset the fake measurement reports for the ORIGINAL cues (first cue at 1000 ms)
        self.disagree = False             # the halves disagree: no offset
        self._orig = (sync.speech_spans, sync.measure_offset, sync.load_detector)
        sync.speech_spans, sync.measure_offset, sync.load_detector = self._fake_spans, self._fake_measure, lambda: None
        self.addCleanup(self._restore_vad)

    def _restore_vad(self):
        sync.speech_spans, sync.measure_offset, sync.load_detector = self._orig

    def _fake_spans(self, video, duration_ms=None):
        if self.speech_error:
            raise sync.vad.SpeechError(self.speech_error)
        return [(0, 1000)], 600000, [(0, 300000), (300000, 600000)]

    def _fake_measure(self, cues, spans, duration_ms, regions=None):
        """Deterministic stand-in for vad.measure: the test subtitle's first cue starts at 1000 ms, so a shifted copy is
        recognised and reported as that much closer to in sync."""
        if self.disagree:
            return {"offset_ms": None, "halves": [self.shift_ms, 0], "z": [2.0, 2.0], "why": "halves disagree"}
        off = self.shift_ms - (cues[0][0] - 1000)
        return {"offset_ms": off, "halves": [off, off], "z": [5.0, 5.0], "why": ""}

    def _make_plex_db(self, path):
        if os.path.exists(path):
            os.unlink(path)
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE library_sections (id INTEGER PRIMARY KEY, name TEXT, section_type INTEGER);
            CREATE TABLE metadata_items (id INTEGER PRIMARY KEY, library_section_id INTEGER,
                metadata_type INTEGER, parent_id INTEGER, guid TEXT, title TEXT,
                'index' INTEGER, year INTEGER, added_at INTEGER);
            CREATE TABLE media_items (id INTEGER PRIMARY KEY, metadata_item_id INTEGER, duration INTEGER);
            CREATE TABLE media_parts (id INTEGER PRIMARY KEY, media_item_id INTEGER, file TEXT);
            CREATE TABLE media_streams (id INTEGER PRIMARY KEY, stream_type_id INTEGER,
                media_item_id INTEGER, media_part_id INTEGER, codec TEXT, language TEXT,
                url TEXT, forced INTEGER);
            CREATE TABLE metadata_item_views (account_id INTEGER, metadata_item_id INTEGER, guid TEXT, viewed_at INTEGER);
            CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT);
            """
        )
        db.executemany(
            "INSERT INTO library_sections VALUES (?,?,?)",
            [(10, "Shows", 2), (9, "Cartoons", 2), (7, "Hentai", 2)],
        )
        db.executemany(
            "INSERT INTO metadata_items VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (100, 10, 2, None, "plex://show/show1", "Show", None, 2020, 0),
                (101, 10, 3, 100, "plex://season/s1", "Season 1", 1, 2020, 0),
                (1001, 10, 4, 101, "plex://episode/e1", "Pilot", 1, 2020, 0),
                (1002, 10, 4, 101, "plex://episode/e2", "Second", 2, 2020, 0),
                (200, 9, 2, None, "plex://show/cartoon", "Cartoon", None, 2021, 0),
                (201, 9, 3, 200, "plex://season/c1", "Season 1", 1, 2021, 0),
                (2001, 9, 4, 201, "plex://episode/c1", "Cartoon Ep", 1, 2021, 0),
                (700, 7, 2, None, "plex://show/hentai", "Hentai Show", None, 2022, 0),
                (701, 7, 3, 700, "plex://season/h1", "Season 1", 1, 2022, 0),
                (7001, 7, 4, 701, "plex://episode/h1", "Hentai Ep", 1, 2022, 0),
            ],
        )
        db.executemany(
            "INSERT INTO media_items VALUES (?,?,?)",
            [(5001, 1001, 1800000), (5002, 1002, 1800000),
             (6001, 2001, 600000), (8001, 7001, 600000)],
        )
        db.executemany(
            "INSERT INTO media_parts VALUES (?,?,?)",
            [
                (9001, 5001, "/shows/Season 01/Show - S01E01.mkv"),
                (9002, 5002, "/other/Show - S01E02.mkv"),
                (9003, 6001, "/shows/Season 01/Show - S01E01.mkv"),
                (9004, 7001, "/hentai/ep.mkv"),
            ],
        )
        streams = [
            (1, 1, 5001, 9001, "h264", None),
            (2, 2, 5001, 9001, "aac", "eng"),
            (3, 3, 5001, 9001, "srt", "en"),
            (4, 1, 5002, 9002, "h264", None),
            (5, 2, 5002, 9002, "aac", "eng"),
            (6, 3, 5002, 9002, "pgs", "en"),
            (7, 2, 6001, 9003, "aac", "eng"),
            (8, 2, 8001, 9004, "aac", "eng"),
        ]
        db.executemany(
            "INSERT INTO media_streams VALUES (?,?,?,?,?,?,NULL,0)", streams
        )
        db.execute("INSERT INTO accounts VALUES (1,'maria')")
        db.execute("INSERT INTO metadata_item_views (account_id, metadata_item_id, guid, viewed_at) SELECT 1, 2001, guid, ? FROM metadata_items WHERE id = 2001", (int(time.time()),))
        db.commit()
        db.close()

    def _run(self, module, **values):
        fn = getattr(module, "main", None)
        if fn is None:
            for name in ("run_scan", "scan_library", "scan_db", "run_sync",
                         "sync_all", "process_incoming", "run_place", "scan", "process"):
                fn = getattr(module, name, None)
                if fn is not None:
                    break
        if fn is None:
            self.fail("%s has no main()" % module.__name__)
        kwargs = {}
        for name in inspect.signature(fn).parameters:
            low = name.lower()
            if "rule" in low:
                kwargs[name] = self.rules
            elif "db" in low or "plex" in low:
                kwargs[name] = str(common.PLEX_DB_NIGHTLY)
        return fn(**kwargs)

    def _load(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _items(self):
        return self._load(common.LIBRARY).get("items", {})

    def _row(self, plex_id):
        for row in self._items().values():
            if str(row.get("plex_id")) == str(plex_id):
                return row
        self.fail("no row for plex_id %s" % plex_id)

    def _write_library(self):
        row = {
            "key": self.KEY, "plex_id": 1001, "kind": "episode", "library": "Shows",
            "title": "Pilot", "show": "Show", "season": 1, "episode": 1,
            "plex_path": "/shows/Season 01/Show - S01E01.mkv", "path": self.ep_video,
            "status": "ok", "duration_ms": 600000,
            "audio": [{"lang": "eng", "codec": "aac", "channels": 2}],
            "subs": [], "views": {}, "need": ["en"],
        }
        common.write_json(common.LIBRARY, {"at": int(time.time()), "items": {self.KEY: row}})

    def _write_pair(self, folder, step="find", confidence=0.9, lang=None, srt=None):
        lang = lang or self.LANG
        common.ensure_dir(folder)
        base = os.path.join(folder, "%s.%s" % (self.KEY, lang))
        with open(base + ".srt", "w", encoding="utf-8") as fh:
            fh.write(srt if srt is not None else _own_srt(self.KEY, step, confidence))
        common.write_json(base + ".json", {
            "key": self.KEY, "lang": lang, "step": step,
            "source": "opensubtitles", "confidence": confidence, "at": int(time.time()),
        })
        return base

    def _sidecar(self, lang=None):
        return os.path.join(os.path.dirname(self.ep_video),
                            "Show - S01E01.%s.srt" % (lang or self.LANG))

    # ---- scan ---------------------------------------------------------

    def test_scan_skips_hentai(self):
        self._run(scan)
        items = self._items()
        self.assertFalse(any(str(r.get("plex_id")) == "7001" for r in items.values()))
        self.assertFalse(any(r.get("library") == "Hentai" for r in items.values()))
        for kind in ("episode", "ep"):
            try:
                key = common.item_key(kind, 7001)
            except Exception:
                continue
            self.assertNotIn(key, items)
        self.assertNotIn("Hentai", json.dumps(items))

    def test_scan_maps_paths_and_marks_unmapped(self):
        self._run(scan)
        mapped = self._row(1001)
        self.assertEqual(mapped["status"], "ok")
        self.assertEqual(os.path.realpath(mapped["path"]), os.path.realpath(self.ep_video))
        unmapped = self._row(1002)
        self.assertEqual(unmapped["status"], "unmapped")
        self.assertIsNone(unmapped.get("path"))
        self.assertEqual(unmapped.get("need"), [])

    def test_image_subtitles_count_as_present(self):
        self._run(scan)
        row = self._row(1002)
        self.assertTrue(any(s.get("codec") == "pgs" for s in row.get("subs", [])))
        self.assertNotIn("en", row.get("need", []))

    def test_need_es_only_for_listed_users(self):
        self._run(scan)
        cartoon = self._row(2001).get("need", [])
        self.assertIn("en", cartoon)
        self.assertIn("es-419", cartoon)
        for plex_id in (1001, 1002):
            need = self._row(plex_id).get("need", [])
            self.assertNotIn("es-419", need)

    def test_coverage_counts(self):
        self._run(scan)
        coverage = self._load(common.COVERAGE)
        libs = coverage["libraries"]
        self.assertNotIn("Hentai", libs)
        shows = libs["Shows"]
        self.assertEqual(shows["items"], 2)
        self.assertEqual(shows["en"], 2)
        self.assertEqual(shows["none"], 0)
        self.assertEqual(shows["generatable"], 0)
        cartoons = libs["Cartoons"]
        self.assertEqual(cartoons["items"], 1)
        self.assertEqual(cartoons["en"], 0)
        self.assertEqual(cartoons["none"], 1)
        self.assertEqual(cartoons["generatable"], 1)
        self.assertEqual(coverage["generatable_total"], 1)

    # ---- sync ---------------------------------------------------------

    def _route(self, step, confidence, lang="en"):
        meta = {"key": self.KEY, "lang": lang, "step": step, "confidence": confidence, "source": "x"}
        return sync.route(meta, [(1000, 2000, "Hi")], self.rules, 0)

    def test_route_thresholds(self):
        self.assertEqual(self._route("gen", 0.9), "queue")
        self.assertEqual(self._route("gen", 0.7), "review")
        self.assertIn(self._route("gen", 0.5), ("discard", "discarded"))
        self.assertEqual(self._route("find", 0.1), "queue")
        self.assertEqual(self._route("gen", 0.99, "es-419"), "review")

    def _run_sync(self, step="find", confidence=0.9, lang=None):
        self._write_library()
        common.write_json(getattr(common, "ES_KEPT",
                                  os.path.join(common.STATE, "es_kept.json")),
                          {"kept": 0})
        self._write_pair(common.INCOMING, step=step, confidence=confidence, lang=lang)
        self._run(sync)

    def _pair_result(self, folder):
        return os.path.exists(os.path.join(folder, "%s.en.srt" % self.KEY))

    def test_sync_check_keeps_original_within_offset(self):
        self.shift_ms = 200
        self._run_sync()
        self.assertTrue(self._pair_result(common.QUEUE))
        with open(os.path.join(common.QUEUE, "%s.en.srt" % self.KEY), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("00:00:01,000", text)
        self.assertNotIn("00:00:01,200", text)
        self.assertIn("Hello", text)

    def test_sync_check_shifts_the_original_cues_on_a_large_offset(self):
        self.shift_ms = 5000
        self._run_sync()
        self.assertTrue(self._pair_result(common.QUEUE))
        with open(os.path.join(common.QUEUE, "%s.en.srt" % self.KEY), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("00:00:06,000", text)
        self.assertIn("Hello", text)             # the ORIGINAL cues shifted by the measured offset; the text is untouched
        meta = json.load(open(os.path.join(common.QUEUE, "%s.en.json" % self.KEY)))
        self.assertEqual(meta["offset_ms"], 5000)
        self.assertEqual(meta["measured"]["offset_ms"], 5000); self.assertEqual(meta["measured"]["after_shift"]["offset_ms"], 0)
        for n in os.listdir(common.QUEUE): os.unlink(os.path.join(common.QUEUE, n))
        self.shift_ms = 200                                                      # in sync: offset_ms stays null (no Fix-timing button)
        self._run_sync()
        meta = json.load(open(os.path.join(common.QUEUE, "%s.en.json" % self.KEY)))
        self.assertIsNone(meta["offset_ms"]); self.assertEqual(meta["measured"]["offset_ms"], 200)

    def test_sync_generated_files_keep_whispers_timeline(self):
        """A generated file is never shifted (an audio aligner once "fixed" an in-sync Whisper file by 4.1 s); the measurement
        is logged beside it, and a found file whose halves disagree is a timing verdict: discarded, never review."""
        self.shift_ms = 30000
        self._run_sync(step="gen", confidence=0.99)
        self.assertTrue(self._pair_result(common.QUEUE)); self.assertFalse(self._pair_result(common.REVIEW))
        text = open(os.path.join(common.QUEUE, "%s.en.srt" % self.KEY), encoding="utf-8").read()
        self.assertIn("00:00:01,000", text); self.assertNotIn("00:00:31,000", text)
        meta = json.load(open(os.path.join(common.QUEUE, "%s.en.json" % self.KEY)))
        self.assertIsNone(meta["offset_ms"]); self.assertEqual(meta["measured"]["offset_ms"], 30000)
        last = [json.loads(l) for l in open(common.DONE_LOG, "rb") if l.strip()][-1]
        self.assertEqual(last["measured_ms"], 30000)
        for n in os.listdir(common.QUEUE): os.unlink(os.path.join(common.QUEUE, n))
        self.shift_ms, self.disagree = 0, True
        self._run_sync()
        self.assertFalse(self._pair_result(common.QUEUE)); self.assertFalse(self._pair_result(common.REVIEW)); self.assertFalse(self._pair_result(common.INCOMING))
        last = [json.loads(l) for l in open(common.DONE_LOG, "rb") if l.strip()][-1]
        self.assertEqual(last["result"], "discarded"); self.assertEqual(last["why"], "timing not confirmed (halves disagree)")
        self.disagree = False

    def test_sync_timing_policy_shift_then_recheck_or_discard(self):
        """15 s..60 s: shifted and re-checked, then queued; over auto_shift_max_ms: discarded, never review (the owner's rule)."""
        self.shift_ms = 30000
        self._run_sync()
        self.assertTrue(self._pair_result(common.QUEUE), "a 30 s offset is shifted, re-checked and queued")
        self.assertFalse(self._pair_result(common.REVIEW))
        text = open(os.path.join(common.QUEUE, "%s.en.srt" % self.KEY), encoding="utf-8").read()
        self.assertIn("00:00:31,000", text)
        for n in os.listdir(common.QUEUE): os.unlink(os.path.join(common.QUEUE, n))
        self.shift_ms = 90000
        self._run_sync()
        self.assertFalse(self._pair_result(common.QUEUE)); self.assertFalse(self._pair_result(common.REVIEW)); self.assertFalse(self._pair_result(common.INCOMING))
        last = [json.loads(l) for l in open(common.DONE_LOG, "rb") if l.strip()][-1]
        self.assertEqual((last["step"], last["result"]), ("sync", "discarded")); self.assertTrue(last["why"].startswith("off by 90.0 s"), last["why"])

    def test_sync_review2_kimi_findings(self):
        """No phantom cues after a shift; an unconfirmed measurement is a timing verdict; an inf confidence is bad metadata;
        es_kept as a string is tolerated; an owner-requested shift that still misses goes to review; >10 min is a timing
        verdict (discard)."""
        body = "1\n00:00:01,000 --> 00:00:02,000\nhi\n\n2\n00:00:20,000 --> 00:00:21,000\nthere\n\n3\n00:00:30,000 --> 00:00:31,000\nagain\n"
        cues = common.parse_srt(body.encode())
        rules = {"find": {"sync_max_offset_ms": 1500, "auto_shift_max_ms": 60000}}
        sync.measure_offset = lambda cs, sp, d, r=None: {"offset_ms": -5000 if cs[0][0] == 1000 else 0, "why": ""}
        ok, off, out, why = sync.apply_timing(cues, {"offset_ms": -5000}, [], 600000, rules)
        self.assertTrue(ok); self.assertEqual(off, -5000)
        self.assertEqual(out[0][:2], (15000, 16000), out)                   # cue 1 dropped (it would end before 0), cue 2 moved
        self.assertNotIn((0, 1), [c[:2] for c in out])
        ok, off, out, why = sync.apply_timing(cues, {"offset_ms": 601000}, [], 600000, rules)
        self.assertEqual((ok, off, why), (False, None, "off by 601.0 s"), "beyond 10 min is still a timing verdict")
        m = {"offset_ms": 20000}
        sync.measure_offset = lambda cs, sp, d, r=None: {"offset_ms": 0, "why": ""}
        ok, off, out, why = sync.apply_timing(cues, m, [], 600000, rules)
        self.assertEqual((ok, off), (True, 20000)); self.assertEqual(m["after_shift"]["offset_ms"], 0)   # the re-measure travels with the verdict
        ok, off, out, why = sync.apply_timing(cues, {"offset_ms": None, "why": "halves disagree"}, [], 600000, rules)
        self.assertEqual((ok, off, why), (False, None, "timing not confirmed (halves disagree)"))
        sync.measure_offset = lambda cs, sp, d, r=None: {"offset_ms": 4000, "why": ""}      # the re-check still misses
        ok, off, out, why = sync.apply_timing(cues, {"offset_ms": 20000}, [], 600000, rules)
        self.assertEqual((ok, off), (False, None)); self.assertTrue(why.startswith("off by 20.0 s; shift did not fit (still off by 4.0 s)"), why)
        sync.measure_offset = self._fake_measure
        self.assertTrue(sync._number({"kept": "0"}, ("kept",), 0) == 0)
        self._write_library()
        base = self._write_pair(common.INCOMING, confidence=float("inf"))
        self._run(sync)
        self.assertFalse(self._pair_result(common.QUEUE)); self.assertFalse(self._pair_result(common.REVIEW))
        common.write_json(common.ES_KEPT, {"kept": "0"})
        self.shift_ms = 90000
        base = self._write_pair(common.INCOMING)
        meta = json.load(open(base + ".json")); meta["shifted_by"] = 30000; common.write_json(base + ".json", meta)
        self._run(sync)
        self.assertTrue(self._pair_result(common.REVIEW), "an owner-requested shift that still misses is explained in review")
        why = json.load(open(os.path.join(common.REVIEW, "%s.en.json" % self.KEY))).get("why", "")
        self.assertIn("off by", why); self.assertIn("after a 30.0 s fix", why)
        for n in os.listdir(common.REVIEW): os.unlink(os.path.join(common.REVIEW, n))
        self.shift_ms = 0

    def test_regated_file_that_fails_the_gate_drops_the_old_placement(self):
        """repair_placed.py sends a placed sidecar back through the gate with regated=True: when the new check cannot confirm it,
        the pair is discarded AND a delete decision is written so place removes the stale sidecar; an owner's decision is kept."""
        self._write_library()
        base = self._write_pair(common.INCOMING)
        meta = json.load(open(base + ".json")); meta["regated"] = True; common.write_json(base + ".json", meta)
        self.disagree = True
        self._run(sync)
        self.assertFalse(self._pair_result(common.QUEUE)); self.assertFalse(self._pair_result(common.REVIEW))
        dec = json.load(open(os.path.join(common.DECISIONS, "%s.json" % self.KEY)))
        self.assertEqual(dec["decision"], "delete"); self.assertIn("halves disagree", dec["why"])
        common.write_json(os.path.join(common.DECISIONS, "%s.json" % self.KEY), {"decision": "exclude", "at": "x", "by": "page"})
        base = self._write_pair(common.INCOMING)
        meta = json.load(open(base + ".json")); meta["regated"] = True; common.write_json(base + ".json", meta)
        self._run(sync)
        self.assertEqual(json.load(open(os.path.join(common.DECISIONS, "%s.json" % self.KEY)))["decision"], "exclude")
        os.unlink(os.path.join(common.DECISIONS, "%s.json" % self.KEY))
        self.disagree = False
        base = self._write_pair(common.INCOMING)                                  # a plain found file never writes a decision
        self.disagree = True
        self._run(sync)
        self.assertFalse(os.path.exists(os.path.join(common.DECISIONS, "%s.json" % self.KEY)))
        self.disagree = False

    def test_sync_check_failure_to_review(self):
        self.speech_error = "ffmpeg exit 1"
        self._run_sync()
        self.assertTrue(self._pair_result(common.REVIEW))
        with open(os.path.join(common.REVIEW, "%s.en.json" % self.KEY), encoding="utf-8") as fh:
            self.assertIn("ffmpeg exit 1", fh.read())

    def test_sync_routes_generated_by_rules_thresholds(self):
        """route() reads rules.thresholds (not generate.*): a confidence between the two goes to review, above keep to queue."""
        self.rules["thresholds"] = {"keep": 0.95, "review": 0.90}
        common.write_json(common.RULES, self.rules)
        self._run_sync(step="gen", confidence=0.92)
        self.assertTrue(self._pair_result(common.REVIEW))
        self.assertFalse(self._pair_result(common.QUEUE))

    def test_sync_rejects_pair_without_valid_metadata(self):
        self._write_library()
        base = self._write_pair(common.INCOMING)
        os.unlink(base + ".json")
        self._run(sync)
        self.assertFalse(self._pair_result(common.QUEUE))
        self.assertFalse(self._pair_result(common.REVIEW))
        self.assertFalse(os.path.exists(base + ".srt"))
        with open(common.DONE_LOG, encoding="utf-8") as fh:
            self.assertIn("metadata missing", fh.read())

    def test_lexical_media_path_never_touches_the_disk(self):
        """scan has no media access: a mapped path counts as ok without a stat; traversal and foreign roots are unmapped."""
        MEDIA_ROOT = os.path.join(TMPHOME, "media")
        rules = {"media_roots": [MEDIA_ROOT]}
        ghost = os.path.join(MEDIA_ROOT, "Shows", "Ghost", "nothing-here.mkv")
        self.assertEqual(common.lexical_media_path(ghost, rules), ghost)
        for bad in (os.path.join(MEDIA_ROOT, "..", "etc", "passwd"), "/etc/passwd", MEDIA_ROOT + "/a//b.mkv",
                    MEDIA_ROOT + "/a/\x07b.mkv", "relative/x.mkv", None, 5):
            self.assertIsNone(common.lexical_media_path(bad, rules), bad)

    def test_scan_marks_stale_and_keeps_fields(self):
        common.write_json(common.COVERAGE, {"at": "x", "stale": False, "libraries": {"TV": {"items": 3}}, "generatable_total": 1})
        scan._mark_stale()
        cov = common.read_json(common.COVERAGE, 1 << 20)
        self.assertEqual((cov["stale"], cov["libraries"]["TV"]["items"], cov["generatable_total"]), (True, 3, 1))

    # ---- place --------------------------------------------------------

    def _place(self):
        self._write_library()
        return place.place_one(self.KEY, self.LANG, self._items(), self.rules, {})

    def test_place_refuses_foreign_sidecar(self):
        with open(self._sidecar(), "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:01,000 --> 00:00:02,000\nforeign\n")
        self._write_pair(common.QUEUE)
        ok, _why = self._place()
        self.assertFalse(ok)
        self.assertTrue(self._pair_result(common.REVIEW))
        self.assertFalse(self._pair_result(common.QUEUE))

    def test_place_writes_and_records_manifest(self):
        self._write_pair(common.QUEUE)
        ok, why = self._place()
        self.assertTrue(ok, why)
        self.assertTrue(os.path.isfile(self._sidecar()))
        placed = common.marks(self.KEY)["placed"][self.LANG]
        self.assertEqual(placed["path"], self._sidecar())
        self.assertEqual(placed["inode"], os.stat(self._sidecar()).st_ino)
        self.assertEqual(placed["sha256"], common.sha256_file(self._sidecar()))
        self.assertFalse(self._pair_result(common.QUEUE))

    def test_place_replaces_only_own_file(self):
        self._write_pair(common.QUEUE)
        ok, why = self._place()
        self.assertTrue(ok, why)
        self.assertTrue(place.own_sidecar(self._sidecar(), self.KEY, self.LANG))
        os.unlink(os.path.join(common.MARKS, "%s.json" % self.KEY))
        self.assertFalse(place.own_sidecar(self._sidecar(), self.KEY, self.LANG))

    def test_own_sidecar_rules_path_rejects_foreign_and_swapped(self):
        """The fd-based branch (rules given): a foreign file wearing our marker is not ours, and neither is our own
        sidecar once someone swapped its content (sha256 mismatch)."""
        self._write_pair(common.QUEUE)
        ok, why = self._place()
        self.assertTrue(ok, why)
        self.assertTrue(place.own_sidecar(self._sidecar(), self.KEY, self.LANG, self.rules))
        foreign = self._sidecar("es")
        with open(foreign, "w") as fh:
            fh.write("; subplot %s opensubtitles 0.9\n1\n00:00:01,000 --> 00:00:02,000\nhi\n" % self.KEY)
        self.assertFalse(place.own_sidecar(foreign, self.KEY, "es", self.rules))
        with open(self._sidecar(), "a") as fh:
            fh.write("\n2\n00:00:03,000 --> 00:00:04,000\nswapped\n")
        self.assertFalse(place.own_sidecar(self._sidecar(), self.KEY, self.LANG, self.rules))

    def test_place_rechecks_exclude(self):
        self._write_pair(common.QUEUE)
        common.ensure_dir(common.DECISIONS)
        common.write_json(os.path.join(common.DECISIONS, "%s.json" % self.KEY),
                          {"decision": "exclude", "at": int(time.time())})
        ok, _why = self._place()
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self._sidecar()))
        self.assertFalse(self._pair_result(common.QUEUE))

    # ---- page decisions ----------------------------------------------

    def _decide(self, decision):
        return place.apply_decision(self.KEY, {"decision": decision,
                                               "lang": self.LANG},
                                    self._items(), self.rules)

    def test_apply_decision_keep_moves_review_to_queue(self):
        self._write_library()
        self._write_pair(common.REVIEW)
        self._decide("keep")
        self.assertTrue(self._pair_result(common.QUEUE))
        self.assertFalse(self._pair_result(common.REVIEW))

    def test_apply_decision_shift_moves_a_corrected_file_back_to_incoming(self):
        """'shift' applies the measured offset_ms to the review file and sends it back through sync's gate (incoming), with
        shifted_by recorded; without a measured offset nothing moves."""
        self._write_library()
        base = self._write_pair(common.REVIEW)
        meta = json.load(open(base + ".json")); meta["offset_ms"] = 5000; meta["why"] = "off by 5.0 s"; common.write_json(base + ".json", meta)
        self._decide("shift")
        self.assertFalse(self._pair_result(common.REVIEW))
        self.assertTrue(self._pair_result(common.INCOMING))
        text = open(os.path.join(common.INCOMING, "%s.en.srt" % self.KEY), encoding="utf-8").read()
        self.assertIn("00:00:06,000 --> 00:00:07,000", text)                 # 1 s + 5 s
        self.assertNotIn("00:00:01,000", text)
        self.assertTrue(text.rstrip("\n").split("\n")[-1].startswith("; subplot " + self.KEY))
        out_meta = json.load(open(os.path.join(common.INCOMING, "%s.en.json" % self.KEY)))
        self.assertEqual(out_meta.get("shifted_by"), 5000); self.assertNotIn("offset_ms", out_meta); self.assertNotIn("why", out_meta)
        for n in os.listdir(common.INCOMING): os.unlink(os.path.join(common.INCOMING, n))
        base = self._write_pair(common.REVIEW)                                 # no offset: stays
        self._decide("shift")
        self.assertTrue(self._pair_result(common.REVIEW)); self.assertFalse(self._pair_result(common.INCOMING))

    def test_apply_decision_delete_removes_own_file(self):
        self._write_library()
        self._write_pair(common.QUEUE)
        ok, why = place.place_one(self.KEY, self.LANG, self._items(), self.rules, {})
        self.assertTrue(ok, why)
        self._decide("delete")
        self.assertFalse(os.path.exists(self._sidecar()))

    def test_apply_decision_exclude_clears_pending(self):
        self._write_library()
        self._write_pair(common.REVIEW)
        self._write_pair(common.QUEUE)
        self._decide("exclude")
        self.assertFalse(self._pair_result(common.REVIEW))
        self.assertFalse(self._pair_result(common.QUEUE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
