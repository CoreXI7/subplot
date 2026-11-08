#!/usr/bin/env python3
"""AUTH=proxy (the default) must behave exactly as before the board's own sign-in existed (SPEC-LOGIN, security rule 1).
Runs this checkout's board and a reference board side by side on the same state and compares the replies to a fixed list
of requests (status, headers minus Date, body). The reference is the commit before sign-in (SUBS_REFERENCE_CODE, a
checkout's subplot/ folder; default: git's v1 tag or the first parent of the sign-in commit, exported to the sandbox).
index.html, subs.js and account.js changed on purpose (the Account section, and a 401 that sends the page to sign in) and are compared only for status.
Also: STATE_DIR/auth is never created, and nothing sign-in related answers."""
import os, shutil, socket, subprocess, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SB = os.environ.get("SUBS_SANDBOX") or os.path.expanduser("~")
REF_COMMIT = os.environ.get("SUBS_REFERENCE_COMMIT", "af6b1c4")   # the last commit before sign-in

REQUESTS = [
    ("GET", "/api/config", None), ("GET", "/api/state", None), ("GET", "/api/missing?q=a", None),
    ("GET", "/api/show/1", None), ("GET", "/base.css", None), ("GET", "/favicon.svg", None),
    ("GET", "/nope", None), ("GET", "/../etc", None), ("GET", "/api/auth/state", None), ("GET", "/login", None),
    ("GET", "/login.js", None), ("GET", "/api/account", None),
    ("POST", "/api/login", b'{"user": "a", "password": "b"}'), ("POST", "/api/setup-account", b'{}'),
    ("POST", "/api/logout", b'{}'), ("POST", "/api/account/password", b'{}'),
    ("POST", "/api/decision", b'{"key": "x", "decision": "keep"}'), ("POST", "/api/request", b'{}'),
    ("POST", "/api/pc", b'{"on": false}'), ("DELETE", "/api/state", None),
]
CHANGED_ON_PURPOSE = {"/", "/account.js", "/subs.js"}   # (subs.js: a 401 with signin sends the page to login)


def reference_code():
    if os.environ.get("SUBS_REFERENCE_CODE"):
        return os.environ["SUBS_REFERENCE_CODE"]
    out = tempfile.mkdtemp(prefix="subplot-ref-", dir=SB)
    git = subprocess.run(["git", "-C", ROOT, "archive", REF_COMMIT, "subplot"], capture_output=True, timeout=60)
    if git.returncode:
        return None
    subprocess.run(["tar", "-x", "-C", out], input=git.stdout, check=True, timeout=60)
    return os.path.join(out, "subplot")


def start(code_dir, state, cfg, sock, extra=None):
    env = {**os.environ, "SUBS_STATE": state, "SUBS_RULES": os.path.join(cfg, "rules.json"), "SUBS_HOST": "subs.example.com",
           "SUBS_SOCK": sock, "SUBS_SOCK_GROUP": "", "SUBS_MARKER_TAG": "subplot", **(extra or {})}
    p = subprocess.Popen([sys.executable, "-I", os.path.join(code_dir, "server.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if os.path.exists(sock):
            return p
        time.sleep(0.05)
    p.kill()
    raise AssertionError(f"board at {code_dir} did not start")


def ask(sock, method, path, body):
    head = f"{method} {path} HTTP/1.1\r\nHost: subs.example.com\r\nX-Forwarded-Proto: https\r\nConnection: close\r\n"
    if body is not None:
        head += f"X-Subs: 1\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(10)
    c.connect(sock)
    c.sendall(head.encode() + b"\r\n" + (body or b""))
    data = b""
    while (b := c.recv(65536)):
        data += b
    c.close()
    raw_head, _, payload = data.partition(b"\r\n\r\n")
    lines = raw_head.decode("latin-1").split("\r\n")
    headers = sorted(l for l in lines[1:] if not l.lower().startswith("date:"))
    return lines[0], headers, payload


class ProxyModeUnchanged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = reference_code()
        if not cls.ref:
            raise unittest.SkipTest("no reference checkout (set SUBS_REFERENCE_CODE)")
        cls.base = tempfile.mkdtemp(prefix="subplot-modes-", dir=SB)
        cls.state, cls.cfg = os.path.join(cls.base, "state"), os.path.join(cls.base, "cfg")
        for d in ("decisions", "requests", "review", "marks", "queue", "incoming", "pc", "tokens"):
            os.makedirs(os.path.join(cls.state, d))
        os.makedirs(cls.cfg)
        with open(os.path.join(cls.cfg, "rules.json"), "w") as f:
            f.write("{}")
        cls.procs = []

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.terminate()
            p.wait(timeout=10)
        shutil.rmtree(cls.base, ignore_errors=True)

    def compare(self, extra):
        tag = "proxy" if extra else "unset"
        new_sock, ref_sock = os.path.join(self.base, f"new-{tag}.sock"), os.path.join(self.base, f"ref-{tag}.sock")
        self.procs.append(start(os.path.join(ROOT, "subplot"), self.state, self.cfg, new_sock, extra))
        self.procs.append(start(self.ref, self.state, self.cfg, ref_sock))
        for method, path, body in REQUESTS + [("GET", p, None) for p in sorted(CHANGED_ON_PURPOSE)]:
            with self.subTest(method=method, path=path, auth=tag):
                new, ref = ask(new_sock, method, path, body), ask(ref_sock, method, path, body)
                if path in CHANGED_ON_PURPOSE:
                    self.assertEqual(new[0].split(" ")[1] in ("200",), True, new[0])
                    continue
                self.assertEqual(new, ref)
        self.assertFalse(os.path.exists(os.path.join(self.state, "auth")), "proxy mode created STATE_DIR/auth")

    def test_auth_unset(self):
        self.compare(None)

    def test_auth_proxy(self):
        self.compare({"SUBS_AUTH": "proxy"})

    def test_anything_else_is_proxy(self):
        """Only the exact word login turns the sign-in on; anything else is proxy (v1)."""
        self.compare({"SUBS_AUTH": "LOGIN "})


if __name__ == "__main__":
    unittest.main()
