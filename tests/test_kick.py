"""A decision saved while subs-place is mid-run is acted on in that same run (the board's .kick can't start a second one)."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/
import json, os, sys, tempfile, unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="subs-kick-test-", dir=os.environ.get("SUBS_SANDBOX", "") if os.path.isdir(os.environ.get("SUBS_SANDBOX", "")) else None)
os.environ["SUBS_STATE"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import place  # noqa: E402


class KickTest(unittest.TestCase):
    def setUp(self):
        for d in (C.DECISIONS, C.QUEUE, C.REVIEW):
            os.makedirs(d, exist_ok=True)
        C.write_json(os.path.join(C.DECISIONS, "ep-1.json"), {"decision": "keep", "note": "", "at": C.now(), "by": "page"})

    def test_press_during_run_is_acted_on(self):
        acted, calls = [], {"n": 0}

        def fake_apply(key, dec, library, rules, env=None):
            acted.append(key)
            d = C.decision(key); d["acted_at"] = C.now(); C.write_json(os.path.join(C.DECISIONS, key + ".json"), d)

        def fake_queue(library, rules, env):
            calls["n"] += 1
            if calls["n"] == 1:   # the second press lands while the first pass is placing files
                C.write_json(os.path.join(C.DECISIONS, "ep-2.json"), {"decision": "keep", "note": "", "at": C.now(), "by": "page"})

        with mock.patch.object(place, "apply_decision", fake_apply), mock.patch.object(place, "queue_pass", fake_queue), \
             mock.patch.object(C, "load_rules", lambda: {}), mock.patch.object(place, "rotate_done_log", lambda: None):
            place.main()
        self.assertEqual(acted, ["ep-1", "ep-2"])
        self.assertEqual(calls["n"], 2)

    def test_stuck_decision_is_capped(self):
        with mock.patch.object(place, "apply_decision", lambda *a: None), mock.patch.object(C, "load_rules", lambda: {}), \
             mock.patch.object(place, "queue_pass", lambda *a: None) as q, mock.patch.object(place, "rotate_done_log", lambda: None):
            n = {"p": 0}
            real = place.decisions_pass
            def counting(*a):
                n["p"] += 1; return real(*a)
            with mock.patch.object(place, "decisions_pass", counting):
                place.main()
        self.assertEqual(n["p"], place.PASSES)


if __name__ == "__main__":
    unittest.main()
