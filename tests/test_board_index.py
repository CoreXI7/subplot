"""The board reads board-index.json (only the page's fields), never the full library.json (2026-11-08: the full one
parsed to ~470 MB and the board, capped at 512 MB, was OOM-killed twice)."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot")]
import json, os, subprocess, sys, tempfile, unittest

TMP = tempfile.mkdtemp(prefix="subs-index-test-", dir=os.environ.get("SUBS_SANDBOX") if os.path.isdir(os.environ.get("SUBS_SANDBOX", "")) else None)
RULES = os.path.join(TMP, "rules.json")
with open(RULES, "w") as f:
    f.write("{}")
os.environ.update(SUBS_STATE=TMP, SUBS_HOST="subs.example", SUBS_RULES=RULES)
import common as C  # noqa: E402
import server  # noqa: E402

FULL = {"at": "2026-11-08T00:00:00Z", "items": {"ep-1": {
    "key": "ep-1", "plex_id": 7, "kind": "ep", "library": "Shows", "show_plex_id": 3, "show": "S", "title": "T",
    "season": 1, "episode": 2, "year": 2020, "need": ["en"], "status": "ok",
    "path": "/media/secret/S/S01E02.mkv", "plex_path": "/data/S01E02.mkv", "audio": [{"lang": "en"}],
    "subs": [], "ids": {"tvdb": 1}, "duration_ms": 1, "added_at": 1, "views": 3, "section_id": 2}}}


class BoardIndex(unittest.TestCase):
    def setUp(self):
        for p in (C.LIBRARY, C.BOARD_INDEX):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        server._cache.update({"at": 0.0, "key": None, "library": None})

    def test_index_keeps_only_board_fields(self):
        C.write_board_index(FULL)
        d = C.read_json(C.BOARD_INDEX, 1 << 20)
        self.assertEqual(set(d["items"]["ep-1"]), set(C.BOARD_FIELDS))
        self.assertNotIn("/media/", json.dumps(d))

    def test_board_reads_the_index_not_the_library(self):
        C.write_json(C.LIBRARY, FULL)                                 # only the full one exists: the board sees nothing
        self.assertEqual(server._library()[0].get("items"), {})
        C.write_board_index(FULL)
        server._cache.update({"at": 0.0})
        items = server._library()[0]["items"]
        self.assertEqual(items["ep-1"]["title"], "T")
        self.assertNotIn("path", items["ep-1"])

    def test_scan_rebuilds_the_index_alone(self):
        C.write_json(C.LIBRARY, FULL)
        r = subprocess.run([sys.executable, "-I", os.path.join(_ROOT, "subplot", "scan.py"), "--board-index"],
                           env={**os.environ}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(set(C.read_json(C.BOARD_INDEX, 1 << 20)["items"]["ep-1"]), set(C.BOARD_FIELDS))


if __name__ == "__main__":
    unittest.main()
