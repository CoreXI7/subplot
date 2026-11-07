"""The marker tag (SUBS_MARKER_TAG): a file written under one tag is ours only under that same tag, and a bad tag stops
the service at import. Each case runs in a child interpreter because the tag is read once when common.py loads."""
import os, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = (
    "import sys; sys.path.insert(0, %r); import common as C\n"
    "data = C.write_srt([(0, 1000, 'hi')], ('ep-1', 'os:1', 0.5))\n"
    "sys.stdout.write(data.decode())\n"
) % os.path.join(ROOT, "subplot")
CHECK = "import sys; sys.path.insert(0, %r); import common as C; print(C.our_marker(sys.stdin.read()))" % os.path.join(ROOT, "subplot")


def child(code, tag, stdin=""):
    env = dict(os.environ, SUBS_MARKER_TAG=tag, SUBS_STATE=os.environ.get("SUBS_SANDBOX") or os.getcwd())
    return subprocess.run([sys.executable, "-I", "-c", code], input=stdin, capture_output=True, text=True, env=env, timeout=60)


class MarkerTagTest(unittest.TestCase):
    def test_written_tag_is_the_last_line(self):
        out = child(CODE, "subplot").stdout
        self.assertTrue(out.rstrip("\n").split("\n")[-1].startswith("; subplot ep-1 os:1 "))

    def test_ours_only_under_the_same_tag(self):
        srt = child(CODE, "old-tag").stdout
        self.assertIn("ep-1", child(CHECK, "old-tag", srt).stdout)
        self.assertEqual(child(CHECK, "subplot", srt).stdout.strip(), "None")

    def test_bad_tag_refuses_to_load(self):
        for bad in ("Subplot", "ab", "x y", "tag;rm", "a" * 40, ""):
            self.assertNotEqual(child(CODE, bad).returncode, 0, bad)


if __name__ == "__main__":
    unittest.main()
