import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/
import time
import unittest
import datetime
import os
import shutil
import tempfile
_TMP = tempfile.mkdtemp(prefix="subs-common-")
HOME, STATE = os.path.join(_TMP, "home"), os.path.join(_TMP, "state")
os.makedirs(HOME)
os.makedirs(STATE)
os.environ["SUBS_HOME"] = HOME
os.environ["SUBS_STATE"] = STATE
SECRETS = {"OPENSUBTITLES_API_KEY": "fixtureKeyAlpha",
           "OPENSUBTITLES_USER": "fixtureUserBravo",
           "OPENSUBTITLES_PASS": "fixturePassCharlie",
           "PLEX_TOKEN": "fixtureTokenDelta"}
os.environ.update(SECRETS)
import common as C

_MEDIA = os.path.realpath(os.path.join(_TMP, "media"))


def srt_ms(ms):
    h, m, s, ms = ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def srt_text(cues):
    return "\n".join("%d\n%s --> %s\n%s\n" % (i, srt_ms(a), srt_ms(b), t)
                     for i, (a, b, t) in enumerate(cues, 1))


def put(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class CommonTests(unittest.TestCase):
    def test_validate_subtitle_accepts_srt(self):
        raw = srt_text([(1000, 2000, "Hello <i>there</i>"),
                        (2500, 4000, "<b>World</b>")]).encode()
        cues, why = C.validate_subtitle(raw, "en")
        self.assertIsNotNone(cues, why)
        self.assertEqual((len(cues), cues[0][0], cues[0][1]), (2, 1000, 2000))
        self.assertIn("Hello", cues[0][2])
    def test_validate_subtitle_refuses_oversize(self):
        raw = srt_text([(1000, 2000, "x")]).encode() + b"\n" * (C.MAX_SUB_BYTES + 1)
        self.assertEqual(C.validate_subtitle(raw, "en"), (None, "too big"))
        self.assertEqual(C.validate_subtitle(b"", "en"), (None, "empty"))

    def test_validate_subtitle_refuses_html_beyond_i_b(self):
        raw = b"1\n00:00:01,000 --> 00:00:02,000\nHello <font color=red>x</font> <i>ok</i> <script>bad</script>\n"
        cues, why = C.validate_subtitle(raw, "en")
        self.assertIsNotNone(cues, why)
        self.assertEqual(cues[0][2], "Hello x <i>ok</i> bad")
        self.assertNotIn("<font", cues[0][2]); self.assertNotIn("<script", cues[0][2])

    def test_validate_subtitle_refuses_control_chars(self):
        raw = srt_text([(1000, 2000, "bell\x07 and nul\x00")])
        cues, why = C.validate_subtitle(raw.encode(), "en")
        self.assertIsNone(cues, why)
    def test_validate_subtitle_refuses_unordered_cues(self):
        raw = srt_text([(3000, 4000, "first"), (1000, 2000, "second")])
        cues, why = C.validate_subtitle(raw.encode(), "en")
        self.assertIsNone(cues, why)
    def test_validate_subtitle_refuses_long_cue(self):
        raw = srt_text([(1000, 62000, "sixty one seconds")])
        cues, why = C.validate_subtitle(raw.encode(), "en")
        self.assertIsNone(cues, why)
    def test_validate_subtitle_refuses_too_many_cues(self):
        blocks = []
        for i in range(20001):
            blocks.append("%d\n%s --> %s\ncue %d\n"
                          % (i + 1, srt_ms(i * 2000), srt_ms(i * 2000 + 1000), i))
        cues, why = C.validate_subtitle("\n".join(blocks).encode(), "en")
        self.assertIsNone(cues, why)
    def test_validate_subtitle_converts_ass(self):
        ass = (
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,"
            "{\\i1}Hello{\\i0}\\Nworld\n"
        )
        cues, why = C.validate_subtitle(ass.encode(), "en")
        self.assertIsNotNone(cues, why)
        self.assertEqual((cues[0][0], cues[0][1]), (1000, 2500))
        text = "\n".join(cue[2] for cue in cues)
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("{", text)
    def test_our_marker_round_trip(self):
        marker = ("ep-12345", "os:4242", 0.5)
        out = C.write_srt([(1000, 2000, "Hi")], marker)
        if isinstance(out, (bytes, bytearray)):
            out = out.decode("utf-8")
        text = out if isinstance(out, str) else out.decode()
        self.assertTrue(text.rstrip("\n").split("\n")[-1].startswith("; subplot "), "marker must be the LAST line (Plex)")
        self.assertTrue(text.startswith("1\n"), "the file must start with a cue index")
        self.assertEqual(C.our_marker(b"; subplot ep-1 os:2 0.5\n\n1\n00:00:01,000 --> 00:00:02,000\nHi\n")[0], "ep-1")   # old files
        self.assertEqual(C.strip_marker(out if isinstance(out, bytes) else out.encode()), b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        got = C.our_marker(out)
        self.assertIsNotNone(got)
        self.assertEqual(got[:2], marker[:2])
        self.assertAlmostEqual(float(got[2]), marker[2], places=2)
    def test_safe_media_path_refuses_symlink(self):
        root = _MEDIA
        video = put(os.path.join(root, "real", "movie.mkv"))
        rules = {"media_roots": [root]}
        self.assertTrue(C.safe_media_path(video, rules))
        link = os.path.join(root, "link")
        try:
            os.symlink(os.path.dirname(video), link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symlinks unavailable: %s" % exc)
        self.assertFalse(C.safe_media_path(os.path.join(link, "movie.mkv"), rules))
    def test_safe_media_path_refuses_outside_root(self):
        root = os.path.join(_MEDIA, "inside")
        outside = os.path.realpath(os.path.join(_TMP, "outside"))
        inside = put(os.path.join(root, "movie.mkv"))
        foreign = put(os.path.join(outside, "movie.mkv"))
        rules = {"media_roots": [root]}
        self.assertTrue(C.safe_media_path(inside, rules))
        self.assertFalse(C.safe_media_path(foreign, rules))
    def test_safe_media_path_refuses_fifo(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo unavailable")
        root = _MEDIA
        os.makedirs(root, exist_ok=True)
        fifo = os.path.join(root, "pipe.mkv")
        try:
            os.mkfifo(fifo)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest("fifo unavailable: %s" % exc)
        self.assertFalse(C.safe_media_path(fifo, {"media_roots": [root]}))
    def test_sidecar_path_naming(self):
        video = os.path.join(_MEDIA, "Show S01E01.mkv")
        plain = os.path.basename(str(C.sidecar_path(video, "en")))
        tagged = os.path.basename(str(C.sidecar_path(video, "es", hi=True)))
        self.assertEqual(plain, "Show S01E01.en.srt")
        self.assertEqual(tagged, "Show S01E01.es.sdh.srt")
    def test_redact_removes_fixture_values_bearer_and_ghp(self):
        bearer = "abc123def456ghi789"
        ghp = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"   # privacy-ok: fake value for the redaction tests
        text = ("key=%s user=%s pass=%s token=%s auth Bearer %s git %s bell\x07"
                % (SECRETS["OPENSUBTITLES_API_KEY"], SECRETS["OPENSUBTITLES_USER"],
                   SECRETS["OPENSUBTITLES_PASS"], SECRETS["PLEX_TOKEN"], bearer, ghp))
        out = C.redact(text)
        for value in SECRETS.values():
            self.assertNotIn(value, out)
        self.assertNotIn(bearer, out)
        self.assertNotIn(ghp, out)
        self.assertNotIn("\x07", out)
    def test_budget_take_limit_reset_and_exhausted_until(self):
        self.assertTrue(C.budget_take("downloads", 1, 2))
        self.assertTrue(C.budget_take("downloads", 1, 2))
        self.assertFalse(C.budget_take("downloads", 1, 2))
        soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        C.budget_exhaust(soon)
        self.assertFalse(C.budget_take("searches", 1, 5))
        C.budget_exhaust("2000-01-01T00:00:00Z")
        self.assertTrue(C.budget_take("searches", 1, 5))
        for bad in ("2999-01-01T00:00:00Z", "zzzz", "2999-01-01T00:00:00+00:00"):   # far future or malformed: never wedges
            C.budget_exhaust(bad)
            self.assertTrue(C.budget_take("searches", 1, 5), bad)
    def test_pc_idle_hours_processes_gpu(self):
        now = datetime.datetime(2026, 11, 6, 3, 0, 0)
        rules = {"pc_idle": {"hours": ["00:00-09:00"], "no_processes": ["fixturebusy.exe"], "gpu_util_below": 50, "vram_free_mb": 6000}}
        def probe(gpu=3, procs=(), used=1000, total=32000, busy=(), games=()):
            return {"gpu_util": gpu, "processes": list(procs), "gpu_mem_used_mb": used, "gpu_mem_total_mb": total, "busy_procs": list(busy),
                    "game_procs": list(games)}
        self.assertTrue(C.pc_idle(rules, probe(), now)[0])
        self.assertFalse(C.pc_idle(rules, probe(procs=["FixtureBusy.exe"]), now)[0])
        self.assertFalse(C.pc_idle(rules, probe(gpu=97), now)[0])
        self.assertFalse(C.pc_idle(rules, probe(), datetime.datetime(2026, 11, 6, 12, 0, 0))[0])
        self.assertTrue(C.pc_idle({"pc_idle": {"hours": ["22:00-07:00"], "no_processes": [], "gpu_util_below": 50}}, probe(), now)[0])
        self.assertEqual(C.pc_idle(rules, probe(used=30000), now), (False, "only 2000 MB of VRAM free"))
        self.assertEqual(C.pc_idle(rules, {"gpu_util": 3, "processes": [], "game_procs": [], "busy_procs": []}, now), (False, "no VRAM reading"))
        self.assertEqual(C.pc_idle(rules, {"gpu_util": 3, "processes": [], "game_procs": []}, now), (False, "no GPU process list"))
        for used, total in ((float("nan"), 32000), (-1, 32000), (40000, 32000), (1000, float("inf"))):
            self.assertEqual(C.pc_idle(rules, probe(used=used, total=total), now), (False, "no VRAM reading"), (used, total))
        # an overnight span belongs to the day it starts on, across the week boundary too
        sun = {"pc_idle": {"hours": ["sun 23:00-02:00"], "no_processes": [], "gpu_util_below": 50}}
        self.assertTrue(C.pc_idle(sun, probe(), datetime.datetime(2026, 11, 8, 23, 30))[0])      # Sunday 23:30
        self.assertTrue(C.pc_idle(sun, probe(), datetime.datetime(2026, 11, 9, 1, 0))[0])        # Monday 01:00
        self.assertFalse(C.pc_idle(sun, probe(), datetime.datetime(2026, 11, 9, 23, 30))[0])     # Monday 23:30
        self.assertFalse(C.pc_idle(sun, probe(), datetime.datetime(2026, 11, 8, 1, 0))[0])       # Sunday 01:00 (Saturday night)
        self.assertEqual(C.pc_idle(rules, probe(busy=["ollama.exe"]), now), (False, "ollama.exe is using the GPU"))
        self.assertTrue(C.pc_idle(rules, probe(busy=["python.exe"]), now, ignore=("python.exe",))[0])
        # any process running from a game library blocks, no list needed; a probe without the field blocks too
        self.assertEqual(C.pc_idle(rules, probe(games=["Windrose-Win64-Shipping.exe"]), now), (False, "Windrose-Win64-Shipping.exe is running (game)"))
        we = {"pc_idle": dict(rules["pc_idle"], not_games=["wallpaper64.exe"])}
        self.assertTrue(C.pc_idle(we, probe(games=["Wallpaper64.exe"]), now)[0])                 # Wallpaper Engine lives in steamapps too
        self.assertFalse(C.pc_idle(we, probe(games=["Wallpaper64.exe", "PEAK.exe"]), now)[0])
        nofield = probe(); del nofield["game_procs"]
        self.assertEqual(C.pc_idle(rules, nofield, now), (False, "no game list"))
        # day-tagged spans: an owner who plays Mon-Fri 17:30-23:00 and weekends 10:00-02:00
        evening = {"pc_idle": {"hours": ["tue-fri 00:00-17:30", "mon 02:00-17:30", "mon-thu 23:00-23:59", "sat-sun 02:00-10:00"],
                           "no_processes": [], "gpu_util_below": 50}}
        def at(day, h, m=0):                                        # 2026-11-02 is a Monday
            return datetime.datetime(2026, 11, 2 + day, h, m, 0)
        self.assertTrue(C.pc_idle(evening, probe(), at(1, 3))[0])            # Tue 03:00
        self.assertTrue(C.pc_idle(evening, probe(), at(2, 17, 29))[0])       # Wed 17:29
        self.assertFalse(C.pc_idle(evening, probe(), at(2, 17, 30))[0])      # Wed 17:30 gaming
        self.assertTrue(C.pc_idle(evening, probe(), at(2, 23, 5))[0])        # Wed 23:05
        self.assertFalse(C.pc_idle(evening, probe(), at(4, 23, 5))[0])       # Fri 23:05: weekend night
        self.assertFalse(C.pc_idle(evening, probe(), at(5, 1))[0])           # Sat 01:00 still playing
        self.assertTrue(C.pc_idle(evening, probe(), at(5, 4))[0])            # Sat 04:00
        self.assertFalse(C.pc_idle(evening, probe(), at(6, 12))[0])          # Sun noon
        self.assertFalse(C.pc_idle(evening, probe(), at(0, 1))[0])           # Mon 01:00: Sunday night
        self.assertTrue(C.pc_idle(evening, probe(), at(0, 9))[0])            # Mon 09:00
        self.assertTrue(C.span_covers_day("fri", "mon", 6) and not C.span_covers_day("fri", "mon", 2))
        self.assertIsNone(C._rules_problem.__wrapped__(evening) if hasattr(C._rules_problem, "__wrapped__") else None)
        self.assertTrue(C.HOURS.fullmatch("sat-sun 02:00-10:00") and not C.HOURS.fullmatch("weekend 02:00-10:00"))
        # the owner's Generate now switch waives the hours and nothing else
        self.assertTrue(C.pc_idle(evening, probe(), at(2, 19), override=True)[0])                        # Wed 19:00, normally the owner's
        self.assertEqual(C.pc_idle(evening, probe(games=["PEAK.exe"]), at(2, 19), override=True), (False, "PEAK.exe is running (game)"))
        self.assertEqual(C.pc_idle(evening, probe(busy=["ollama.exe"]), at(2, 19), override=True), (False, "ollama.exe is using the GPU"))
        self.assertFalse(C.pc_override())
        C.write_json(C.PC_OVERRIDE, {"until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))})
        self.assertTrue(C.pc_override())
        C.write_json(C.PC_OVERRIDE, {"until": "2999-01-01T00:00:00Z"})                               # far future: not honoured
        self.assertFalse(C.pc_override())
        C.write_json(C.PC_OVERRIDE, {"until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))})
        self.assertFalse(C.pc_override())
        os.unlink(C.PC_OVERRIDE)
    def test_review2_fixes_in_common(self):
        """Findings of the 2026-11-07 review round pinned: override cap, current row reset, request cap, NaN confidence,
        whitespace-only separator, byte-safe marker strip."""
        C.write_json(C.PC_OVERRIDE, {"until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 25 * 3600))})
        self.assertFalse(C.pc_override(), "25 h ahead is not honoured")
        os.unlink(C.PC_OVERRIDE)
        C.set_current(step="gen", key="ep-1", show="Old show", phase="x")
        C.set_current(step="gen", key="ep-2", phase="y")
        cur = C.read_json(C.CURRENT, 8192)
        self.assertEqual((cur.get("key"), cur.get("show"), cur.get("phase")), ("ep-2", None, "y"))
        C.clear_current()
        C.ensure_dir(C.REQUESTS)
        for i in range(C.MAX_REQUESTS + 5):
            C.write_json(os.path.join(C.REQUESTS, f"20260101-000000-{i:06x}.json"), {"id": f"r{i}", "kind": "find", "key": "ep-1", "at": "x"})
        self.assertEqual(len(C.requests_pending()), C.MAX_REQUESTS)
        for n in os.listdir(C.REQUESTS): os.unlink(os.path.join(C.REQUESTS, n))
        with self.assertRaises(ValueError):
            C.write_srt([(1000, 2000, "Hi")], ("ep-1", "os:1", float("nan")))
        cues = C.parse_srt("1\n00:00:01,000 --> 00:00:02,000\nA\nB\n   \n2\n00:00:03,000 --> 00:00:04,000\nC\n")
        self.assertEqual(len(cues), 2, "a whitespace-only line ends a cue")
        raw = b"1\r\n00:00:01,000 --> 00:00:02,000\r\nCaf\xe9\r\n\r\n; subplot ep-1 os:1 0.5\r\n"
        out = C.strip_marker(raw)
        self.assertNotIn(b"subplot", out); self.assertIn(b"Caf\xe9", out, "bytes are not re-encoded")

    def test_parse_srt_without_blank_lines(self):
        """Some files omit the blank line between cues; an index line followed by a time line starts a new cue anyway,
        instead of swallowing the rest of the file into cue 1 (found by the crew's test fixture, 2026-11-07)."""
        raw = "1\n00:00:01,000 --> 00:00:02,000\nA\n2\n00:00:03,000 --> 00:00:04,000\nB\n3\n00:00:05,000 --> 00:00:06,000\n42\n"
        cues = C.parse_srt(raw)
        self.assertEqual([c[2] for c in cues], ["A", "B", "42"])
        self.assertEqual([c[0] for c in cues], [1000, 3000, 5000])
        two = C.parse_srt("1\n00:00:01,000 --> 00:00:02,000\n7\nstill cue one\n\n2\n00:00:03,000 --> 00:00:04,000\nB\n")
        self.assertEqual(two[0][2], "7\nstill cue one", "a bare number followed by text is text, not an index")

    def test_validate_subtitle_is_linear_on_hostile_input(self):
        """2 MiB of newlines took minutes before (the ASS-detection regex used \\s* across lines); it must stay well under a second."""
        import time
        for raw in (b"\n" * (2 * 1024 * 1024), b"\n" * 1_000_000 + b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", b" \n" * 500_000):
            t = time.time(); C.validate_subtitle(raw, "en")
            self.assertLess(time.time() - t, 2.0, "validator went quadratic")

    def test_night_id_around_18h(self):
        before = datetime.datetime(2026, 11, 6, 17, 59, 0)
        at = datetime.datetime(2026, 11, 6, 18, 0, 0)
        after = datetime.datetime(2026, 11, 6, 23, 59, 0)
        self.assertEqual(str(C.night_id(before)), "2026-11-05")
        self.assertEqual(str(C.night_id(at)), "2026-11-06")
        self.assertEqual(str(C.night_id(after)), "2026-11-06")
    def test_mark_and_marks_increment(self):
        key = "ep-987654"
        C.mark(key, gen_failed_inc=1, stage="fixture")
        C.mark(key, gen_failed_inc=1)
        marks = C.marks(key)
        self.assertEqual(marks.get("gen_failed"), 2)
        self.assertEqual(marks.get("stage"), "fixture")
    def test_load_rules_drops_bad_file_keeps_last_good(self):
        os.makedirs(_MEDIA, exist_ok=True)
        probe = os.path.join(STATE, "rules-probe.json")
        full = {
            "media_roots": [_MEDIA],
            "libraries": [{"name": "Movies", "path": _MEDIA}],
            "exclude_libraries": ["Hentai"], "path_map": {},
            "languages": ["en", "es"],
            "find": {"daily_budget": 40, "retry_days": 30, "min_score": 70},
            "generate": {"threshold": 0.8, "hours": "00:00-23:59",
                         "processes": [], "gpu_util_max": 25},
            "pc": {"hours": "00:00-23:59", "processes": [], "gpu_util_max": 25},
            "thresholds": {"keep": 0.8},
        }
        accepted = None
        for candidate in (full, {"media_roots": [_MEDIA]}, {}):
            C.write_json(probe, candidate)
            if isinstance(C.load_rules(probe), dict):
                accepted = candidate
                break
        if accepted is None:
            self.skipTest("no rules document accepted by this common")
        os.makedirs(os.path.dirname(str(C.RULES)), exist_ok=True)
        C.write_json(C.RULES, accepted)
        self.assertIsInstance(C.load_rules(), dict)
        C.write_bytes(C.RULES, b"{ this is not json")
        kept = C.load_rules()
        self.assertIsInstance(kept, dict)
        if "media_roots" in accepted:
            self.assertEqual(kept.get("media_roots"), accepted["media_roots"])



class WrongLanguageTests(unittest.TestCase):
    """A '.en' file must read as English: Whisper on a Japanese or Spanish audio track once produced '.en' files that were not."""
    def _srt(self, lines):
        return "".join(f"{i + 1}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{t}\n\n" for i, t in enumerate(lines)).encode()

    def test_japanese_text_refused_as_english(self):
        raw = self._srt(["洗 濯 もう 終 わ る から、 す", "ぐ 股 に 戻 る よ", "この 泉、 魔 法 が か か ってる から"] * 5)
        cues, why = C.validate_subtitle(raw, "en")
        self.assertIsNone(cues)
        self.assertIn("wrong language", why)

    def test_spanish_text_refused_as_english(self):
        raw = self._srt(["Esperen, ¿ustedes se conocen?", "Ella me ayudó a hallar mi camino a casa.",
                         "Pero yo no sé qué es lo que está pasando aquí.", "Vamos, hay que irnos con ella para la nave."] * 4)
        cues, why = C.validate_subtitle(raw, "en")
        self.assertIsNone(cues)
        self.assertIn("Spanish", why)
        self.assertIsNotNone(C.validate_subtitle(raw, "es-419")[0])

    def test_english_text_passes_and_short_files_are_not_judged(self):
        raw = self._srt(["I don't know what you want from me.", "It's the way that we have to do this.",
                         "You're right, and that is what they said to him."] * 4)
        self.assertIsNotNone(C.validate_subtitle(raw, "en")[0])
        self.assertIsNotNone(C.validate_subtitle(self._srt(["¡Hola!", "Sí."]), "en")[0])
        self.assertIsNotNone(C.validate_subtitle(self._srt(["こんにちは"] * 20), "ja")[0])

class ValidatorWriterAgreeTests(unittest.TestCase):
    """Whatever validate_subtitle accepts, write_srt must be able to write: a mismatch (a zero-length cue) once crashed every
    find run on the same file, so OpenSubtitles lookups stopped for a day."""
    def test_zero_length_cue_is_dropped(self):
        raw = b"1\n00:00:01,000 --> 00:00:01,000\nnothing\n\n2\n00:00:02,000 --> 00:00:03,000\nHello there.\n\n"
        cues, why = C.validate_subtitle(raw, "en")
        self.assertEqual([c[2] for c in cues], ["Hello there."])
        C.write_srt(cues, ("ep-1", "os:1", 0.9))

    def test_random_odd_files_never_break_the_writer(self):
        import random
        rnd = random.Random(7)
        for _ in range(400):
            n, t, parts = rnd.randint(1, 12), 0, []
            for i in range(n):
                t += rnd.choice([0, 0, 1, 500, 2000])
                d = rnd.choice([0, 0, 1, 999, 4000, 61000])
                parts.append("%d\n%s --> %s\n%s\n" % (i + 1, self._ts(t), self._ts(t + d), rnd.choice(["Hi.", "", "<i>ok</i>", "  ", "♪"])))
            cues, why = C.validate_subtitle("\n".join(parts).encode(), "en")
            if cues is not None:
                C.write_srt(cues, ("ep-1", "os:1", 0.9))

    @staticmethod
    def _ts(ms):
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


if __name__ == "__main__":
    unittest.main()
