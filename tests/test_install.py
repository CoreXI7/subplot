"""Tests for tools/install.py, written from _brief/install.md alone."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import install as I

import contextlib
import io
import tempfile
import unittest

STRICT_PATH_KEYS = (
    "CODE_DIR",
    "STATE_DIR",
    "CONFIG_DIR",
    "VENV",
    "SOCKET",
    "OVERLAY_DIR",
)
ALL_PATH_KEYS = STRICT_PATH_KEYS + ("PLEX_DB",)

UNITS = (
    "scan.service",
    "scan.timer",
    "find.service",
    "find.timer",
    "sync.service",
    "sync.timer",
    "place.service",
    "place.timer",
    "place.path",
    "gen.service",
    "gen.timer",
    "board.service",
)

_example = None


def example():
    global _example
    if _example is None:
        path = os.path.join(_ROOT, "config", "subplot.conf.example")
        if not os.path.isfile(path):
            path = os.path.join(_ROOT, "subplot.conf.example")
        with open(path, encoding="utf-8") as handle:
            _example = handle.read()
    return _example


def _key_of(line):
    if line.startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0]


def with_value(key, value):
    lines = []
    replaced = False
    for line in example().splitlines():
        if _key_of(line) == key:
            lines.append("%s=%s" % (key, value))
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append("%s=%s" % (key, value))
    return "\n".join(lines) + "\n"


def without_key(key):
    lines = [line for line in example().splitlines() if _key_of(line) != key]
    return "\n".join(lines) + "\n"


def plus_line(line):
    return example().rstrip("\n") + "\n" + line + "\n"


class ParseConfTests(unittest.TestCase):

    def assertRefused(self, text):
        with self.assertRaises(I.Refused):
            I.parse_conf(text)

    def test_example_parses(self):
        conf = I.parse_conf(example())
        self.assertIsInstance(conf, dict)
        self.assertEqual(conf["UNIT_PREFIX"], "subplot")

    def test_every_required_key_missing_is_refused(self):
        for key, spec in I.PATTERNS.items():
            if spec[1]:
                with self.subTest(key=key):
                    self.assertRefused(without_key(key))

    def test_unknown_key_refused(self):
        self.assertRefused(plus_line("MYSTERY_KEY=/srv/x"))

    def test_repeated_key_refused(self):
        self.assertRefused(plus_line("CODE_DIR=/opt/other"))

    def test_line_without_equals_refused(self):
        self.assertRefused(plus_line("this line has no equals sign"))

    def test_path_with_dotdot_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt/../x"))

    def test_path_with_double_slash_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt//x"))

    def test_path_with_space_refused(self):
        for key in STRICT_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt/sub plot"))

    def test_path_with_quote_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt/sub'plot"))

    def test_path_with_percent_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt/sub%plot"))

    def test_path_with_dollar_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt/sub$plot"))

    def test_path_with_backslash_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt/sub\\plot"))

    def test_path_with_newline_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/opt/sub\nplot"))

    def test_single_slash_path_refused(self):
        for key in ALL_PATH_KEYS:
            with self.subTest(key=key):
                self.assertRefused(with_value(key, "/"))

    def test_plex_db_with_space_accepted(self):
        value = "/var/backups/plex/my library.db"
        conf = I.parse_conf(with_value("PLEX_DB", value))
        self.assertEqual(conf["PLEX_DB"], value)

    def test_plex_db_with_quote_refused(self):
        self.assertRefused(with_value("PLEX_DB", "/var/backups/plex/lib'rary.db"))

    def test_media_roots_two_items_accepted(self):
        value = "/mnt/media1 /mnt/media2"
        conf = I.parse_conf(with_value("MEDIA_ROOTS", value))
        self.assertEqual(conf["MEDIA_ROOTS"], value)

    def test_media_roots_seventeen_items_refused(self):
        value = " ".join("/mnt/media%d" % n for n in range(1, 18))
        self.assertRefused(with_value("MEDIA_ROOTS", value))

    def test_pc_ip_octet_over_255_refused(self):
        self.assertRefused(with_value("PC_IP", "10.0.0.300"))

    def test_pc_ip_public_address_refused(self):
        self.assertRefused(with_value("PC_IP", "8.8.8.8"))

    def test_pc_root_with_backslash_refused(self):
        self.assertRefused(with_value("PC_ROOT", "C:\\Subplot"))

    def test_unit_prefix_other_than_allowed_refused(self):
        self.assertRefused(with_value("UNIT_PREFIX", "other"))

    def test_run_user_uppercase_refused(self):
        self.assertRefused(with_value("RUN_USER", "Root"))

    def test_run_user_with_space_refused(self):
        self.assertRefused(with_value("RUN_USER", "me dia"))


class RenderTests(unittest.TestCase):

    def test_tokens_replaced(self):
        out = I.render("ExecStart=@VENV@/bin/python\n", {"VENV": "/opt/v"})
        self.assertIn("ExecStart=/opt/v/bin/python", out)

    def test_empty_optional_token_line_dropped(self):
        out = I.render(
            "Environment=SUBS_OVERLAY=@OVERLAY_DIR@\nKeep=1\n",
            {"OVERLAY_DIR": ""},
        )
        self.assertNotIn("SUBS_OVERLAY", out)
        self.assertIn("Keep=1", out)

    def test_media_root_line_repeated_in_order(self):
        out = I.render(
            "BindReadOnlyPaths=@MEDIA_ROOT@\n",
            {"MEDIA_ROOTS": "/mnt/a /mnt/b"},
        )
        lines = [line for line in out.splitlines() if line]
        self.assertEqual(
            lines,
            ["BindReadOnlyPaths=/mnt/a", "BindReadOnlyPaths=/mnt/b"],
        )

    def test_unknown_token_refused(self):
        with self.assertRaises(I.Refused):
            I.render("X=@NOPE@\n", {})

    def test_value_line_with_comment_refused(self):
        with self.assertRaises(I.Refused):
            I.render(
                "ExecStart=@VENV@/bin/python  # start it\n",
                {"VENV": "/opt/v"},
            )

    def test_systemd_call_filter_token_kept(self):
        text = "SystemCallFilter=@system-service\n"
        self.assertEqual(I.render(text, {}), text)


class RenderAllTests(unittest.TestCase):

    def _tokens(self):
        conf = I.parse_conf(example())
        return conf, I.tokens(conf, conf["RUN_USER"], "/home/" + conf["RUN_USER"])

    def _dest(self, sandbox):
        return tempfile.TemporaryDirectory(dir=sandbox or None)

    def test_render_all_writes_twelve_named_units(self):
        conf, tokens = self._tokens()
        expected = {"%s-%s" % (conf["UNIT_PREFIX"], unit) for unit in UNITS}
        sandbox = os.environ.get("SUBS_SANDBOX")
        with self._dest(sandbox) as dest:
            rendered = I.render_all(tokens, dest)
            self.assertEqual(len(rendered), 12)
            self.assertEqual(
                {os.path.basename(path) for _, path in rendered},
                expected,
            )
            self.assertEqual(set(os.listdir(dest)), expected)

    def test_render_all_leaves_no_token(self):
        _, tokens = self._tokens()
        sandbox = os.environ.get("SUBS_SANDBOX")
        with self._dest(sandbox) as dest:
            I.render_all(tokens, dest)
            for name in sorted(os.listdir(dest)):
                with open(os.path.join(dest, name), encoding="utf-8") as handle:
                    text = handle.read()
                with self.subTest(unit=name):
                    self.assertNotRegex(text, r"@[A-Z]")


class MainTests(unittest.TestCase):

    def test_check_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf_path = os.path.join(tmp, "subplot.conf")
            with open(conf_path, "w", encoding="utf-8") as handle:
                handle.write(example())
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = I.main(["--check", conf_path])
            self.assertIsInstance(result, int)


if __name__ == "__main__":
    unittest.main()
