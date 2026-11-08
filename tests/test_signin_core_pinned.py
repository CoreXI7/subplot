"""The vendored signin-core copy matches its pin, and with AUTH=proxy nothing touches the sign-in's data folder
(signin-core SPEC.md, Security model 2 and 5)."""
import os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = os.path.join(ROOT, "subplot")
sys.path.insert(0, CODE)
from signin_core import verify  # noqa: E402


class Pinned(unittest.TestCase):
    def test_vendored_copy_matches_its_pin(self):
        ok, problems = verify.check(CODE)
        self.assertTrue(ok, problems)

    def test_pin_matches_the_signin_core_release_when_present(self):
        src = os.path.expanduser("~/projects/signin-core/MANIFEST.sha256")
        if not os.path.exists(src):
            self.skipTest("signin-core repo not here")
        with open(src, "rb") as a, open(os.path.join(CODE, verify.PIN_NAME), "rb") as b:
            self.assertEqual(a.read(), b.read(), "subplot/signin_core.pin differs from signin-core's MANIFEST.sha256: run tools/sync.py")


PROBE = r"""
import os, sys
data = os.path.join(os.environ["SUBS_STATE"], "auth")
seen = []
def hook(event, args):
    if event in ("open", "os.listdir", "os.mkdir", "os.stat", "os.scandir", "os.chmod", "os.remove", "os.rename") and args:
        p = args[0]
        if isinstance(p, (str, bytes)) and os.fsdecode(p).startswith(data):
            seen.append((event, os.fsdecode(p)))
sys.addaudithook(hook)
sys.path.insert(0, sys.argv[1])
import server
srv_dir = os.path.dirname(os.environ["SUBS_SOCK"])
print("SEEN", seen)
"""


class ProxyModeTouchesNothing(unittest.TestCase):
    def test_import_in_proxy_mode_opens_nothing_under_auth(self):
        base = tempfile.mkdtemp(prefix="pin-", dir=os.environ.get("SUBS_SANDBOX") or None)
        env = {**os.environ, "SUBS_STATE": os.path.join(base, "state"), "SUBS_SOCK": os.path.join(base, "b.sock"),
               "SUBS_HOST": "subs.example", "SUBS_AUTH": "proxy", "SUBS_RULES": os.path.join(base, "rules.json")}
        r = subprocess.run([sys.executable, "-I", "-c", PROBE, CODE], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("SEEN []", r.stdout, r.stdout[-500:])
        self.assertFalse(os.path.exists(os.path.join(base, "state", "auth")))


if __name__ == "__main__":
    unittest.main()
