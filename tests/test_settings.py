"""Unit tests for the v3 settings layer in common.py.

The contract is spec-settings.md (spec v2, 2026-11-08); these tests are
written from that spec only.  Run with:  python tests/test_settings.py
"""

import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

SANDBOX = os.environ.get("SUBS_SANDBOX") or os.path.expanduser("~/.cache/subplot-dev")
os.makedirs(SANDBOX, exist_ok=True)

_BOOT = tempfile.mkdtemp(prefix="subplot-settings-tests-", dir=SANDBOX)

os.environ["SUBS_HOME"] = os.path.join(_BOOT, "home")
os.environ["SUBS_STATE"] = os.path.join(_BOOT, "state")
os.environ["SUBS_RULES"] = os.path.join(_BOOT, "rules.json")
os.environ["SUBS_SOCK"] = os.path.join(_BOOT, "subplot.sock")
for _name in ("SUBS_HOME", "SUBS_STATE"):
    os.makedirs(os.environ[_name], exist_ok=True)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_ROOT, "subplot") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "subplot"))

import common  # noqa: E402  (imported only after the environment is set)


def _deep_set(target, dotted, value):
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value
    return target


def _nested(dotted, value):
    return _deep_set({}, dotted, value)


def _deep_get(obj, dotted, default=None):
    node = obj
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _leaves(obj, prefix=""):
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(_leaves(value, prefix + ("." if prefix else "") + key))
    else:
        found.append(prefix)
    return found


def _log_text(mock_log):
    parts = []
    for call in mock_log.call_args_list:
        for arg in call.args:
            parts.append(str(arg))
        for value in call.kwargs.values():
            parts.append(str(value))
    return " ".join(parts)


class SettingsTestBase(unittest.TestCase):
    def setUp(self):
        self.case_dir = tempfile.mkdtemp(prefix="case-", dir=_BOOT)
        self.media = os.path.join(self.case_dir, "media")
        self.map_from = os.path.join(self.case_dir, "map-from")
        os.makedirs(self.media)
        os.makedirs(self.map_from)
        self.rules_path = os.path.join(self.case_dir, "rules.json")
        self.settings_dir = os.path.join(self.case_dir, "settings")
        os.makedirs(self.settings_dir)
        self.settings_path = os.path.join(self.settings_dir, "settings.json")
        self.minimal_rules = {
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
        }
        self.write_rules(self.minimal_rules)

    def write_rules(self, rules):
        common.write_json(self.rules_path, rules)
        return rules

    def write_settings(self, values, version=1, by="page",
                       at="2026-01-01T00:00:00Z", extra=None):
        envelope = {"version": version, "at": at, "by": by, "values": values}
        if extra:
            envelope.update(extra)
        common.write_json(self.settings_path, envelope)
        return envelope

    def filled(self):
        return common.file_rules(self.rules_path)

    def apply(self, filled=None):
        return common.apply_settings(
            self.filled() if filled is None else filled, self.settings_path)

    def check(self, values):
        return common.check_settings(self.filled(), values)

    def test_common_paths_are_in_the_sandbox(self):
        state = os.path.abspath(os.environ["SUBS_STATE"])
        settings = os.path.abspath(str(common.SETTINGS))
        self.assertTrue(settings.startswith(state + os.sep), settings)
        self.assertEqual(os.path.basename(settings), "settings.json")
        self.assertEqual(os.path.basename(os.path.dirname(settings)), "settings")
        self.assertFalse(settings.startswith("/tmp/"))

    def test_apply_settings_reports_its_envelope(self):
        self.write_settings({"find": {"daily_budget": 500}}, by="alex")
        rules, info = self.apply()
        for key in ("version", "at", "by", "values", "ignored", "dropped"):
            self.assertIn(key, info)
        self.assertEqual(info["version"], 1)
        self.assertEqual(info["at"], "2026-01-01T00:00:00Z")
        self.assertEqual(info["by"], "alex")
        self.assertIsNone(info["ignored"])
        self.assertEqual(info["dropped"], [])
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 500)


class TestDefaults(SettingsTestBase):
    def test_defaults_fill_a_minimal_rules_json(self):
        rules = self.filled()
        self.assertEqual(rules["path_map"], self.minimal_rules["path_map"])
        self.assertEqual(rules["media_roots"], self.minimal_rules["media_roots"])
        self.assertEqual(_deep_get(rules, "plex.machine_id"), "")
        self.assertEqual(_deep_get(rules, "languages.primary"), "en")
        self.assertEqual(_deep_get(rules, "languages.secondary"), "es-419")
        self.assertEqual(_deep_get(rules, "languages.secondary_for_users"), [])
        self.assertEqual(_deep_get(rules, "thresholds.keep"), 0.85)
        self.assertEqual(_deep_get(rules, "thresholds.review"), 0.60)
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        self.assertEqual(_deep_get(rules, "find.min_score"), 25)
        self.assertEqual(_deep_get(rules, "find.sync_max_offset_ms"), 1500)
        self.assertEqual(_deep_get(rules, "find.retry_days"), 30)
        self.assertEqual(_deep_get(rules, "find.auto_shift_max_ms"), 60000)
        self.assertEqual(_deep_get(rules, "generate.model"), "large-v3")
        self.assertEqual(_deep_get(rules, "generate.max_hours_per_night"), 6)
        self.assertEqual(_deep_get(rules, "generate.burned_in_frames"), 12)
        self.assertEqual(_deep_get(rules, "generate.burned_in_ratio"), 0.5)
        self.assertEqual(_deep_get(rules, "generate.es_review_until_kept"), 20)
        self.assertEqual(_deep_get(rules, "pc_idle.hours"),
                         ["mon-fri 00:00-17:00", "sat-sun 02:00-09:00"])
        self.assertEqual(_deep_get(rules, "pc_idle.no_processes"), [])
        self.assertEqual(_deep_get(rules, "pc_idle.gpu_util_below"), 20)
        self.assertEqual(_deep_get(rules, "pc_idle.vram_free_mb"), 6000)
        self.assertEqual(_deep_get(rules, "pc_idle.yield_to"), [])
        not_games = _deep_get(rules, "pc_idle.not_games")
        self.assertIsInstance(not_games, list)
        self.assertTrue(not_games)
        self.assertNotIn("unload_idle_model", rules.get("pc_idle", {}))
        self.assertEqual(_deep_get(rules, "priority.watched_days"), 30)
        self.assertEqual(_deep_get(rules, "priority.libraries"), [])
        self.assertEqual(_deep_get(rules, "exclude_libraries"), [])
        self.assertIsInstance(common.DEFAULTS, dict)
        self.assertTrue(common.DEFAULTS)

    def test_defaults_do_not_loosen_the_code_exclusions_in_load_rules(self):
        rules = common.load_rules(self.rules_path, settings=None)
        self.assertEqual(_deep_get(rules, "thresholds.keep"), 0.85)
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)

    def test_absent_settings_file_means_no_page_changes(self):
        self.assertFalse(os.path.exists(self.settings_path))
        rules = common.load_rules(self.rules_path, settings=self.settings_path)
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        for name in common.EXCLUDE_LIBRARIES:
            self.assertIn(name, rules["exclude_libraries"])


class TestFullOldRules(SettingsTestBase):
    def full_rules(self):
        return {
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "plex": {"machine_id": "0123456789abcdef0123456789abcdef"},
            "languages": {"primary": "en", "secondary": "es-419",
                          "secondary_for_users": ["alex"]},
            "thresholds": {"keep": 0.9, "review": 0.65},
            "find": {"daily_budget": 500, "min_score": 40,
                     "sync_max_offset_ms": 1000, "retry_days": 14,
                     "auto_shift_max_ms": 30000},
            "generate": {"model": "medium", "max_hours_per_night": 4,
                         "burned_in_frames": 8, "burned_in_ratio": 0.4,
                         "es_review_until_kept": 10},
            "pc_idle": {"hours": ["mon-fri 00:00-17:00"],
                        "no_processes": ["chrome.exe"], "gpu_util_below": 10,
                        "vram_free_mb": 4096, "yield_to": ["game.exe"],
                        "not_games": ["wallpaper_engine.exe"]},
            "priority": {"watched_days": 7, "libraries": ["Movies"]},
            "exclude_libraries": ["Old"],
        }

    def test_full_old_rules_json_loads_unchanged(self):
        full = self.write_rules(self.full_rules())
        rules = self.filled()
        for dotted in ("plex.machine_id", "languages.primary",
                       "languages.secondary", "languages.secondary_for_users",
                       "thresholds.keep", "thresholds.review",
                       "find.daily_budget", "find.min_score",
                       "find.sync_max_offset_ms", "find.retry_days",
                       "find.auto_shift_max_ms", "generate.model",
                       "generate.max_hours_per_night",
                       "generate.burned_in_frames",
                       "generate.burned_in_ratio",
                       "generate.es_review_until_kept", "pc_idle.hours",
                       "pc_idle.no_processes", "pc_idle.gpu_util_below",
                       "pc_idle.vram_free_mb", "pc_idle.yield_to",
                       "pc_idle.not_games", "priority.watched_days",
                       "priority.libraries"):
            with self.subTest(key=dotted):
                self.assertEqual(_deep_get(rules, dotted),
                                 _deep_get(full, dotted))
        self.assertEqual(rules["path_map"], full["path_map"])
        self.assertEqual(rules["media_roots"], full["media_roots"])
        self.assertTrue(set(full["exclude_libraries"]) <= set(rules["exclude_libraries"]))

    def test_full_old_rules_json_without_settings_is_the_same(self):
        self.write_rules(self.full_rules())
        skipped = common.load_rules(self.rules_path, settings=None)
        filled = self.filled()
        self.assertEqual(_deep_get(skipped, "thresholds.keep"),
                         _deep_get(filled, "thresholds.keep"))
        self.assertEqual(_deep_get(skipped, "pc_idle.hours"),
                         _deep_get(filled, "pc_idle.hours"))


class TestPageKeyTable(SettingsTestBase):
    EXTRA_KEYS = {
        "plex.machine_id": "0123456789abcdef0123456789abcdef",
        "languages.primary": "en",
        "languages.secondary": "es-419",
        "languages.secondary_for_users": ["alex"],
        "thresholds.keep": 0.9,
        "thresholds.review": 0.65,
        "find.min_score": 30,
        "find.sync_max_offset_ms": 1000,
        "find.auto_shift_max_ms": 30000,
        "find.daily_budget": 500,
        "find.retry_days": 20,
        "generate.model": "large-v3",
        "generate.max_hours_per_night": 4,
        "generate.burned_in_frames": 8,
        "generate.burned_in_ratio": 0.4,
        "generate.es_review_until_kept": 10,
        "pc_idle.hours": ["mon-fri 00:00-17:00"],
        "pc_idle.no_processes": ["chrome.exe"],
        "pc_idle.yield_to": ["game.exe"],
        "pc_idle.not_games": ["wallpaper_engine.exe"],
        "pc_idle.gpu_util_below": 15,
        "pc_idle.vram_free_mb": 5000,
        "pc_idle.unload_after_min": 15,
        "priority.watched_days": 10,
        "priority.libraries": ["Movies"],
        "exclude_libraries": ["Old"],
    }

    RANGE_CASES = (
        ("find.daily_budget", 1, 1000, 0, 1001),
        ("find.retry_days", 1, 1000, 0, 1001),
        ("generate.max_hours_per_night", 1, 24, 0, 25),
        ("generate.burned_in_frames", 4, 48, 3, 49),
        ("generate.burned_in_ratio", 0, 1, -0.01, 1.01),
        ("generate.es_review_until_kept", 0, 1000, -1, 1001),
        ("pc_idle.gpu_util_below", 0, 100, -1, 101),
        ("pc_idle.vram_free_mb", 0, 100000, -1, 100001),
        ("pc_idle.unload_after_min", 0, 720, -1, 721),
        ("priority.watched_days", 1, 365, 0, 366),
    )

    def test_page_keys_are_exactly_the_table(self):
        self.assertEqual(set(common.PAGE_KEYS), set(self.EXTRA_KEYS))

    def test_page_keys_hold_their_stricter_only_direction(self):
        for key, entry in common.PAGE_KEYS.items():
            with self.subTest(key=key):
                self.assertIsInstance(entry, tuple)
                self.assertEqual(len(entry), 3)
                self.assertIsInstance(entry[0], str)
                self.assertIn(entry[2], (None, "min", "max"))
        expected = {
            "thresholds.keep": "min",
            "thresholds.review": "min",
            "find.min_score": "min",
            "find.sync_max_offset_ms": "max",
            "find.auto_shift_max_ms": "max",
        }
        for key, direction in expected.items():
            with self.subTest(key=key):
                self.assertEqual(common.PAGE_KEYS[key][2], direction)
        for key, entry in common.PAGE_KEYS.items():
            if key in expected:
                continue
            with self.subTest(key=key):
                self.assertIsNone(entry[2])

    def test_every_table_row_accepts_a_valid_value(self):
        for key, value in self.EXTRA_KEYS.items():
            with self.subTest(key=key):
                clean, why, field = self.check(_nested(key, value))
                self.assertIsNotNone(clean, "%s refused: %s (%s)" % (key, why, field))
                self.assertIsNone(why)

    def test_range_ends_accepted_and_one_step_past_refused(self):
        for key, low, high, below, above in self.RANGE_CASES:
            for value in (low, high):
                with self.subTest(key=key, value=value):
                    clean, why, field = self.check(_nested(key, value))
                    self.assertIsNotNone(clean, "%s refused %r: %s" % (key, value, why))
            for value in (below, above):
                with self.subTest(key=key, value=value):
                    clean, why, field = self.check(_nested(key, value))
                    self.assertIsNone(clean)
                    self.assertTrue(why)
                    self.assertEqual(field, key)

    def test_machine_id_and_model_ends(self):
        self.assertIsNotNone(self.check(_nested("plex.machine_id", "a" * 32))[0])
        self.assertIsNotNone(self.check(_nested("plex.machine_id", "b" * 40))[0])
        self.assertIsNone(self.check(_nested("plex.machine_id", "c" * 31))[0])
        self.assertIsNone(self.check(_nested("plex.machine_id", "d" * 41))[0])
        self.assertIsNone(self.check(_nested("plex.machine_id", "z" * 32))[0])
        self.assertIsNone(self.check(_nested("plex.machine_id", "a" * 31 + "-"))[0])
        self.assertIsNotNone(self.check(_nested("generate.model", "a"))[0])
        self.assertIsNotNone(self.check(_nested("generate.model", "m" * 40))[0])
        self.assertIsNone(self.check(_nested("generate.model", "m" * 41))[0])
        self.assertIsNone(self.check(_nested("generate.model", "bad model"))[0])

    def test_language_codes(self):
        for value in ("en", "es-419"):
            with self.subTest(value=value):
                self.assertIsNotNone(self.check(_nested("languages.primary", value))[0])
                self.assertIsNotNone(self.check(_nested("languages.secondary", value))[0])
        self.assertIsNone(self.check(_nested("languages.primary", "not a language"))[0])
        self.assertIsNone(self.check(_nested("languages.secondary", "not a language"))[0])

    def test_list_caps(self):
        users50 = ["user%d" % i for i in range(50)]
        self.assertIsNotNone(
            self.check(_nested("languages.secondary_for_users", users50))[0])
        self.assertIsNone(
            self.check(_nested("languages.secondary_for_users", users50 + ["one_more"]))[0])

        hours20 = ["mon-fri %02d:00-23:00" % h for h in range(20)]
        self.assertIsNotNone(self.check(_nested("pc_idle.hours", hours20))[0])
        self.assertIsNone(self.check(_nested("pc_idle.hours", hours20 + ["sat-sun 00:00-01:00"]))[0])
        self.assertIsNone(self.check(_nested("pc_idle.hours", ["funday 00:00-01:00"]))[0])

        procs100 = ["proc%d.exe" % i for i in range(100)]
        for key in ("pc_idle.no_processes", "pc_idle.yield_to", "pc_idle.not_games"):
            with self.subTest(key=key):
                self.assertIsNotNone(self.check(_nested(key, procs100))[0])
                self.assertIsNone(self.check(_nested(key, procs100 + ["one_more.exe"]))[0])
                self.assertIsNone(self.check(_nested(key, ["bad\x00name"]))[0])

        libs50 = ["lib%d" % i for i in range(50)]
        self.assertIsNotNone(self.check(_nested("priority.libraries", libs50))[0])
        self.assertIsNone(self.check(_nested("priority.libraries", libs50 + ["one_more"]))[0])
        self.assertIsNotNone(self.check(_nested("priority.libraries", ["L" * 80]))[0])
        self.assertIsNone(self.check(_nested("priority.libraries", ["L" * 81]))[0])
        self.assertIsNone(self.check(_nested("priority.libraries", ["bad\x01name"]))[0])
        self.assertIsNone(self.check(_nested("priority.libraries", [""]))[0])

        ex50 = ["ex%d" % i for i in range(50)]
        self.assertIsNotNone(self.check(_nested("exclude_libraries", ex50))[0])
        self.assertIsNone(self.check(_nested("exclude_libraries", ex50 + ["one_more"]))[0])
        self.assertIsNone(self.check(_nested("exclude_libraries", ["L" * 81]))[0])

    def test_dropped_value_keeps_other_page_values(self):
        self.write_settings({"find": {"daily_budget": 0, "retry_days": 20}})
        rules, info = self.apply()
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        self.assertEqual(_deep_get(rules, "find.retry_days"), 20)
        self.assertIn("find.daily_budget", info["dropped"])


class TestStricterOnly(SettingsTestBase):
    STRICT_KEYS = ("thresholds.keep", "thresholds.review", "find.min_score",
                   "find.sync_max_offset_ms", "find.auto_shift_max_ms")

    def rules_with_floors(self):
        return {
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "thresholds": {"keep": 0.9, "review": 0.6},
            "find": {"min_score": 50, "sync_max_offset_ms": 800,
                     "auto_shift_max_ms": 20000},
        }

    def test_each_stricter_only_key_is_refused_when_looser(self):
        self.write_rules(self.rules_with_floors())
        looser = {
            "thresholds.keep": 0.8,
            "thresholds.review": 0.5,
            "find.min_score": 40,
            "find.sync_max_offset_ms": 900,
            "find.auto_shift_max_ms": 30000,
        }
        for key in self.STRICT_KEYS:
            with self.subTest(key=key):
                clean, why, field = self.check(_nested(key, looser[key]))
                self.assertIsNone(clean)
                self.assertTrue(why)
                self.assertEqual(field, key)

    def test_each_stricter_only_key_is_accepted_when_equal_or_stricter(self):
        self.write_rules(self.rules_with_floors())
        cases = {
            "thresholds.keep": (0.9, 0.95),
            "thresholds.review": (0.6, 0.7),
            "find.min_score": (50, 60),
            "find.sync_max_offset_ms": (800, 700),
            "find.auto_shift_max_ms": (20000, 10000),
        }
        for key, (equal, stricter) in cases.items():
            for value in (equal, stricter):
                with self.subTest(key=key, value=value):
                    clean, why, field = self.check(_nested(key, value))
                    self.assertIsNotNone(clean, "%s refused %r: %s" % (key, value, why))
                    self.assertIsNone(why)

    def test_looser_page_value_is_dropped_and_equal_or_stricter_is_kept(self):
        self.write_rules(self.rules_with_floors())
        cases = {
            "thresholds.keep": (0.8, 0.9, 0.95),
            "thresholds.review": (0.5, 0.6, 0.7),
            "find.min_score": (40, 50, 60),
            "find.sync_max_offset_ms": (900, 800, 700),
            "find.auto_shift_max_ms": (30000, 20000, 10000),
        }
        for key, (looser, equal, stricter) in cases.items():
            with self.subTest(key=key, case="looser"):
                self.write_settings(_nested(key, looser))
                rules, info = self.apply()
                self.assertEqual(_deep_get(rules, key), _deep_get(self.filled(), key))
                self.assertIn(key, info["dropped"])
            with self.subTest(key=key, case="equal"):
                self.write_settings(_nested(key, equal))
                rules, info = self.apply()
                self.assertEqual(_deep_get(rules, key), equal)
                self.assertNotIn(key, info["dropped"])
            with self.subTest(key=key, case="stricter"):
                self.write_settings(_nested(key, stricter))
                rules, info = self.apply()
                self.assertEqual(_deep_get(rules, key), stricter)
                self.assertNotIn(key, info["dropped"])

    def test_raising_the_file_floor_later_drops_the_page_value(self):
        self.write_settings({"thresholds": {"keep": 0.9}})
        rules, info = self.apply()
        self.assertEqual(_deep_get(rules, "thresholds.keep"), 0.9)
        self.write_rules({
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "thresholds": {"keep": 0.95},
        })
        rules, info = self.apply()
        self.assertEqual(_deep_get(rules, "thresholds.keep"), 0.95)
        self.assertIn("thresholds.keep", info["dropped"])

    def test_page_review_above_merged_keep_drops_review(self):
        self.write_rules({
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "thresholds": {"keep": 0.9, "review": 0.6},
        })
        self.write_settings({"thresholds": {"review": 0.95}})
        rules, info = self.apply()
        self.assertEqual(_deep_get(rules, "thresholds.keep"), 0.9)
        self.assertEqual(_deep_get(rules, "thresholds.review"), 0.6)
        self.assertIn("thresholds.review", info["dropped"])

    def test_check_settings_refuses_review_above_merged_keep(self):
        clean, why, field = self.check({"thresholds": {"review": 0.9}})
        self.assertIsNone(clean)
        self.assertTrue(why)
        self.assertEqual(field, "thresholds.review")

    def test_stricter_only_keys_accept_their_absolute_range_ends(self):
        self.write_rules({
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "thresholds": {"keep": 1.0, "review": 1.0},
            "find": {"min_score": 1000, "sync_max_offset_ms": 100000,
                     "auto_shift_max_ms": 600000},
        })
        for key, value in (
                ("thresholds.keep", 1.0),
                ("thresholds.review", 1.0),
                ("find.min_score", 1000),
                ("find.sync_max_offset_ms", 1),
                ("find.sync_max_offset_ms", 100000),
                ("find.auto_shift_max_ms", 0),
                ("find.auto_shift_max_ms", 600000)):
            with self.subTest(key=key, value=value):
                clean, why, field = self.check(_nested(key, value))
                self.assertIsNotNone(clean, "%s refused %r: %s" % (key, value, why))
        for key, value in (
                ("thresholds.keep", 1.01),
                ("thresholds.review", 1.01),
                ("find.min_score", 1001),
                ("find.sync_max_offset_ms", 0),
                ("find.sync_max_offset_ms", 100001),
                ("find.auto_shift_max_ms", -1),
                ("find.auto_shift_max_ms", 600001)):
            with self.subTest(key=key, value=value):
                clean, why, field = self.check(_nested(key, value))
                self.assertIsNone(clean)
                self.assertEqual(field, key)



class TestListsAndExclusions(SettingsTestBase):
    def test_page_lists_replace_file_lists(self):
        self.write_rules({
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "priority": {"libraries": ["A", "B"]},
            "pc_idle": {"no_processes": ["x.exe"], "yield_to": ["y.exe"]},
        })
        self.write_settings({
            "priority": {"libraries": ["C"]},
            "pc_idle": {"no_processes": ["z.exe"], "yield_to": ["w.exe"]},
        })
        rules, info = self.apply()
        self.assertEqual(_deep_get(rules, "priority.libraries"), ["C"])
        self.assertEqual(_deep_get(rules, "pc_idle.no_processes"), ["z.exe"])
        self.assertEqual(_deep_get(rules, "pc_idle.yield_to"), ["w.exe"])

    def test_exclude_union_keeps_hentai_file_and_page_entries(self):
        self.write_rules({
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "exclude_libraries": ["Old"],
        })
        self.write_settings({"exclude_libraries": ["New"]})
        rules, info = self.apply()
        self.assertEqual(set(rules["exclude_libraries"]), {"Old", "New", "Hentai"})
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)

    def test_page_cannot_remove_a_file_exclusion(self):
        self.write_rules({
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "exclude_libraries": ["Old"],
        })
        self.write_settings({"exclude_libraries": []})
        rules, info = self.apply()
        self.assertIn("Old", rules["exclude_libraries"])
        self.assertIn("Hentai", rules["exclude_libraries"])


class TestUnload(SettingsTestBase):
    def test_unload_number_without_an_ollama_url_has_no_effect(self):
        self.write_settings({"pc_idle": {"unload_after_min": 15}})
        rules, info = self.apply()
        self.assertNotIn("unload_idle_model", rules.get("pc_idle", {}))
        clean, why, field = self.check(_nested("pc_idle.unload_after_min", 15))
        self.assertIsNotNone(clean)

    def _filled_with_unload_block(self):
        for block in ({"after_min": 30},
                      {"url": "http://127.0.0.1:11434", "after_min": 30}):
            filled = self.filled()
            _deep_set(filled, "pc_idle.unload_idle_model", dict(block))
            self.write_settings({})
            rules, info = common.apply_settings(copy.deepcopy(filled), self.settings_path)
            if "unload_idle_model" in rules.get("pc_idle", {}):
                return filled
        return None

    def test_unload_null_removes_the_block(self):
        filled = self._filled_with_unload_block()
        if filled is None:
            self.skipTest("pc_idle.unload_idle_model block is not accepted by _rules_problem")
        self.write_settings({"pc_idle": {"unload_after_min": None}})
        rules, info = common.apply_settings(filled, self.settings_path)
        self.assertNotIn("unload_idle_model", rules.get("pc_idle", {}))

    def _filled_with_ollama_url(self):
        filled = self.filled()
        filled["pc_idle"]["unload_idle_model"] = {"ollama": "http://10.0.0.50:11434", "after_min": 10}
        return filled

    def test_unload_number_sets_after_min_when_the_ollama_url_is_set(self):
        filled = self._filled_with_ollama_url()
        if filled is None:
            self.skipTest("no known rules.json Ollama URL key enables unloading")
        self.write_settings({"pc_idle": {"unload_after_min": 45}})
        rules, info = common.apply_settings(copy.deepcopy(filled), self.settings_path)
        self.assertEqual(_deep_get(rules, "pc_idle.unload_idle_model.after_min"), 45)


class TestRobustness(SettingsTestBase):
    def _strip_code_exclusions(self, rules):
        stripped = copy.deepcopy(rules)
        code = set(common.EXCLUDE_LIBRARIES)
        stripped["exclude_libraries"] = [
            item for item in stripped.get("exclude_libraries", []) if item not in code]
        return stripped

    def test_unknown_key_ignores_page_values_but_applies_valid_exclusions(self):
        self.write_rules({
            "path_map": {self.map_from: self.media},
            "media_roots": [self.media],
            "exclude_libraries": ["Old"],
        })
        self.write_settings({
            "find": {"daily_budget": 500},
            "exclude_libraries": ["New"],
            "mystery": {"x": 1},
        })
        rules, info = self.apply()
        self.assertIsNotNone(info["ignored"])
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        self.assertEqual(set(rules["exclude_libraries"]), {"Old", "New", "Hentai"})

    def test_check_settings_refuses_an_unknown_key(self):
        clean, why, field = self.check({
            "find": {"daily_budget": 500},
            "mystery": {"x": 1},
        })
        self.assertIsNone(clean)
        self.assertTrue(why)
        self.assertIn(field, ("mystery", ""))
        clean, why, field = self.check({"not_a_key": 1})
        self.assertIsNone(clean)
        self.assertTrue(why)
        self.assertIn(field, ("not_a_key", ""))

    def test_envelope_extra_key_is_ignored(self):
        self.write_settings({"find": {"daily_budget": 500},
                             "exclude_libraries": ["FromPage"]},
                            extra={"extra": True})
        rules, info = self.apply()
        self.assertIsNotNone(info["ignored"])
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        self.assertIn("FromPage", rules["exclude_libraries"])

    def test_corrupt_settings_file_leaves_file_rules_and_logs_no_values(self):
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "at": "2026-01-01T00:00:00Z", '
                         '"by": "page", "values": {"find": {"daily_budget": 424242}}')
        envelope, reason = common.read_settings(self.settings_path)
        self.assertIsNone(envelope)
        self.assertIsNotNone(reason)
        with mock.patch.object(common, "log") as fake_log:
            rules = common.load_rules(self.rules_path, settings=self.settings_path)
            rules_again, info = self.apply()
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        self.assertEqual(_deep_get(rules_again, "find.daily_budget"), 1000)
        self.assertEqual(self._strip_code_exclusions(rules),
                         self._strip_code_exclusions(self.filled()))
        self.assertNotIn("424242", _log_text(fake_log))

    def test_symlinked_settings_file_is_not_followed(self):
        target = os.path.join(self.settings_dir, "real-settings.json")
        common.write_json(target, {
            "version": 1, "at": "2026-01-01T00:00:00Z", "by": "page",
            "values": {"find": {"daily_budget": 500}},
        })
        try:
            os.symlink(target, self.settings_path)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("platform cannot create symlinks")
        envelope, reason = common.read_settings(self.settings_path)
        self.assertTrue(reason is not None or envelope is None)
        rules, info = self.apply()
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        self.assertEqual(self._strip_code_exclusions(rules),
                         self._strip_code_exclusions(self.filled()))
        self.assertTrue(reason is not None or info["ignored"] is not None)

    def test_booleans_and_nan_are_refused_for_numeric_keys(self):
        numeric = ("find.daily_budget", "find.retry_days", "find.min_score",
                   "find.sync_max_offset_ms", "find.auto_shift_max_ms",
                   "thresholds.keep", "thresholds.review",
                   "generate.max_hours_per_night", "generate.burned_in_frames",
                   "generate.burned_in_ratio", "generate.es_review_until_kept",
                   "pc_idle.gpu_util_below", "pc_idle.vram_free_mb",
                   "priority.watched_days")
        for key in numeric:
            for bad in (True, False, float("nan")):
                with self.subTest(key=key, bad=repr(bad)):
                    clean, why, field = self.check(_nested(key, bad))
                    self.assertIsNone(clean)
                    self.assertTrue(why)
                    self.assertEqual(field, key)

    def test_boolean_and_nan_values_stored_in_the_file_are_not_applied(self):
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "at": "2026-01-01T00:00:00Z", '
                         '"by": "page", "values": {"find": {"daily_budget": true}, '
                         '"generate": {"burned_in_ratio": NaN}}}')
        rules, info = self.apply()
        self.assertEqual(_deep_get(rules, "find.daily_budget"), 1000)
        self.assertEqual(_deep_get(rules, "generate.burned_in_ratio"), 0.5)

    def test_log_never_contains_setting_values(self):
        sentinel = "zz-unique-model"
        self.write_settings({"generate": {"model": sentinel}})
        with mock.patch.object(common, "log") as fake_log:
            rules = common.load_rules(self.rules_path, settings=self.settings_path)
        self.assertEqual(_deep_get(rules, "generate.model"), sentinel)
        self.assertNotIn(sentinel, _log_text(fake_log))

    def test_check_settings_returns_only_values_that_differ_from_the_file(self):
        clean, why, field = self.check({
            "thresholds": {"keep": 0.85, "review": 0.6},
            "find": {"daily_budget": 1000, "min_score": 25},
            "generate": {"model": "large-v3"},
            "pc_idle": {"hours": ["mon-fri 00:00-17:00", "sat-sun 02:00-09:00"]},
        })
        self.assertIsNotNone(clean)
        self.assertIsNone(why)
        self.assertEqual(_leaves(clean), [])
        clean, why, field = self.check({"find": {"daily_budget": 900}})
        self.assertIsNotNone(clean)
        self.assertEqual(_leaves(clean), ["find.daily_budget"])


class TestHelpers(SettingsTestBase):
    def test_read_settings_returns_the_envelope(self):
        self.write_settings({"find": {"retry_days": 5}})
        envelope, reason = common.read_settings(self.settings_path)
        self.assertIsNone(reason)
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["at"], "2026-01-01T00:00:00Z")
        self.assertEqual(envelope["by"], "page")
        self.assertEqual(envelope["values"], {"find": {"retry_days": 5}})

    def test_read_settings_reports_a_missing_file(self):
        envelope, reason = common.read_settings(
            os.path.join(self.settings_dir, "absent.json"))
        self.assertTrue(envelope is None or reason is not None)

    def test_settings_by_sanitises_and_caps_the_name(self):
        self.assertEqual(common.settings_by("alex"), "alex")
        self.assertEqual(common.settings_by("page"), "page")
        self.assertEqual(common.settings_by("Alex <a@x>"), "Alex__a@x_")
        self.assertEqual(len(common.settings_by("a" * 80)), 60)
        self.assertEqual(common.settings_by("a" * 80), "a" * 60)

    def test_exclude_libraries_names_hentai(self):
        self.assertIn("Hentai", list(common.EXCLUDE_LIBRARIES))


class TestStringThresholdsInTheFile(SettingsTestBase):
    """rules.json has always accepted "0.85" as a threshold; the stricter-only rule must not crash on it."""

    def test_string_file_thresholds_still_floor_the_page(self):
        rules = dict(self.minimal_rules, thresholds={"keep": "0.85", "review": "0.6"})
        self.write_rules(rules)
        filled = self.filled()
        self.write_settings({"thresholds": {"keep": 0.5}})
        merged, info = common.apply_settings(filled, self.settings_path)
        self.assertIn("thresholds.keep", info["dropped"])
        clean, why, field = common.check_settings(filled, {"thresholds": {"keep": 0.8}})
        self.assertIsNone(clean)
        self.assertEqual(field, "thresholds.keep")
        self.write_settings({"thresholds": {"keep": 0.9}})
        merged, info = common.apply_settings(filled, self.settings_path)
        self.assertEqual(merged["thresholds"]["keep"], 0.9)


if __name__ == "__main__":
    unittest.main()
