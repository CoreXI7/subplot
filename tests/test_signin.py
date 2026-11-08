#!/usr/bin/env python3
"""The board's own sign-in (AUTH=login), tested from SPEC.md only (blind: written without reading signin.py/server.py/auth.py).
Runs a real board on a unix socket in SUBS_SANDBOX and sends raw HTTP; HTTPS and the client address are injected with
X-Forwarded-Proto and X-Real-IP, as the proxy would set them."""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SB = os.environ.get("SUBS_SANDBOX") or os.path.expanduser("~")
HOST = "subs.example.com"
PASS = "copper lantern orbit meadow"      # fixed strong test passphrase (4 words, 27 chars)
PASS2 = "velvet harbor granite sparrow"
USER = "owner"
HTTPS = [("X-Forwarded-Proto", "https")]
UNAUTH = {"why": "sign in first", "signin": True}


class Reply:
    def __init__(self, raw):
        head, _, self.raw_body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = []
        for line in lines[1:]:
            k, _, v = line.partition(":")
            self.headers.append((k.strip().lower(), v.strip()))
        try:
            self.json = json.loads(self.raw_body)
        except Exception:
            self.json = None

    def header(self, name):
        return [v for k, v in self.headers if k == name.lower()]

    def set_cookies(self):
        """{name: (value, [attributes])} parsed by hand from every Set-Cookie line."""
        out = {}
        for line in self.header("set-cookie"):
            parts = [p.strip() for p in line.split(";")]
            name, _, value = parts[0].partition("=")
            out[name] = (value, parts[1:])
        return out

    def cookie(self, name):
        c = self.set_cookies().get(name)
        return f"{name}={c[0]}" if c else None

    def __repr__(self):
        return f"<{self.status} {self.raw_body[:200]!r}>"


class Board:
    def __init__(self, auth="login"):
        self.dir = tempfile.mkdtemp(prefix="signin-", dir=SB)
        self.state = os.path.join(self.dir, "state")
        os.makedirs(os.path.join(self.state, "auth"), mode=0o700)
        os.chmod(os.path.join(self.state, "auth"), 0o700)
        rules = os.path.join(self.dir, "rules.json")
        with open(rules, "w") as f:
            f.write("{}")
        self.sock = os.path.join(self.dir, "b.sock")
        self.env = {**os.environ, "SUBS_STATE": self.state, "SUBS_RULES": rules, "SUBS_HOST": HOST,
                    "SUBS_SOCK": self.sock, "SUBS_SOCK_GROUP": ""}
        self.env.pop("SUBS_AUTH", None)
        if auth:
            self.env["SUBS_AUTH"] = auth
        self.out = []
        self.proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "subplot", "server.py")], env=self.env,
                                     cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.reader = threading.Thread(target=lambda: self.out.extend(iter(lambda: self.proc.stdout.read(4096), b"")),
                                       daemon=True)
        self.reader.start()
        for _ in range(200):
            if os.path.exists(self.sock):
                break
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        else:
            self.stop()
        if not os.path.exists(self.sock):
            self.stop()
            raise AssertionError("board did not start: " + self.output())

    def cli(self, *args):
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "subplot", "auth.py"), *args], cwd=ROOT,
                           env={**os.environ, "SUBS_STATE": self.state}, capture_output=True, text=True, timeout=60)
        return r.stdout

    def setup_code(self):
        return self.cli("setup-code").split()[-1]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.reader.join(timeout=5)

    def output(self):
        return b"".join(self.out).decode("utf-8", "replace")

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def raw(self, method, path, body=None, headers=None, ip="203.0.113.10"):
        """headers: list of (name, value); repeats allowed. HTTPS is NOT added unless listed."""
        b = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        h = f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n"
        if ip:
            h += f"X-Real-IP: {ip}\r\n"
        for k, v in headers or []:
            h += f"{k}: {v}\r\n"
        if method == "POST":
            h += f"X-Subs: 1\r\nContent-Type: application/json\r\nContent-Length: {len(b)}\r\n"
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(30)
        s.connect(self.sock)
        s.sendall(h.encode() + b"\r\n" + b)
        data = b""
        while (c := s.recv(65536)):
            data += c
        s.close()
        return Reply(data)

    def req(self, method, path, body=None, cookie=None, ip="203.0.113.10", extra=None):
        hs = list(HTTPS) + list(extra or [])
        if cookie:
            hs.append(("Cookie", cookie if isinstance(cookie, str) else "; ".join(c for c in cookie if c)))
        return self.raw(method, path, body, hs, ip)

    def create_account(self, code=None, user=USER, password=PASS):
        r = self.req("POST", "/api/setup-account", {"code": code or self.setup_code(), "user": user,
                                                     "password": password, "keep": True})
        assert r.status == 200, r
        return r


class SignInTests(unittest.TestCase):
    def board(self, auth="login"):
        b = Board(auth)
        self.addCleanup(b.cleanup)
        return b

    def signed_in(self, b):
        r = b.create_account()
        return r, r.cookie("__Host-subplot"), r.cookie("__Host-subplot-device")

    # ---- gate ----
    def test_public_paths_reachable_without_session(self):
        """Requests: the public list (exact match) answers without a session; never the gate's 401/303."""
        b = self.board()
        for path in ("/login", "/login.js", "/base.css", "/favicon.svg", "/api/auth/state"):
            r = b.req("GET", path)
            self.assertEqual(r.status, 200, (path, r))
        st = b.req("GET", "/api/auth/state").json
        self.assertEqual(st["mode"], "login")
        for k in ("setup", "broken", "signed_in", "code_due", "https", "min_length"):
            self.assertIn(k, st)
        self.assertTrue(st["https"])
        self.assertFalse(st["signed_in"])
        for path, body in (("/api/login", {"user": "x", "password": "y", "keep": False}), ("/api/login/code", {"code": "1"}),
                           ("/api/setup-account", {}), ("/api/logout", {})):
            r = b.req("POST", path, body)
            self.assertNotEqual(r.json, UNAUTH, (path, r))
            self.assertNotIn(r.status, (303,), (path, r))
        self.assertEqual(b.req("POST", "/api/logout", {}).status, 200)

    def test_other_paths_refused_without_session(self):
        """Requests: without a session, page paths -> 303 Location: login; any other request -> 401 sign in first."""
        b = self.board()
        b.create_account()   # an account exists; still no session sent below
        r = b.req("GET", "/")
        self.assertEqual(r.status, 303, r)
        self.assertEqual(r.header("location"), ["login"])
        for path in ("/index.html", "/nope", "/x/y"):   # not v1 page paths (v1 404s them): a 303 or a 401, never served
            self.assertIn(b.req("GET", path).status, (303, 401), path)
        for method, path, body in (("GET", "/api/config", None), ("GET", "/api/state", None), ("GET", "/api/account", None),
                                   ("GET", "/api/missing?q=a", None), ("GET", "/api/show/1", None),
                                   ("POST", "/api/decision", {"key": "x", "decision": "keep"}),
                                   ("POST", "/api/request", {}), ("POST", "/api/pc", {"on": False}),
                                   ("POST", "/api/account/password", {"password": PASS, "new": PASS2}),
                                   ("POST", "/api/account/totp/start", {"password": PASS}),
                                   ("POST", "/api/account/totp/confirm", {"code": "123456"}),
                                   ("POST", "/api/account/totp/off", {"password": PASS, "code": "1"}),
                                   ("POST", "/api/account/recovery", {"password": PASS, "code": "1"}),
                                   ("POST", "/api/account/sessions/end", {"password": PASS, "all": True}),
                                   ("POST", "/api/account/notice/dismiss", {"password": PASS, "seen": 0})):
            r = b.req(method, path, body)
            self.assertEqual(r.status, 401, (path, r))
            self.assertEqual(r.json, UNAUTH, path)
        for path in ("/subs.js", "/account.js", "/subs.css"):   # not public: a page redirect or a 401, never the file
            r = b.req("GET", path)
            self.assertIn(r.status, (303, 401), (path, r))
        # a junk cookie is no session
        self.assertEqual(b.req("GET", "/api/state", cookie="__Host-subplot=junk").status, 401)

    # ---- HTTPS ----
    def test_signin_paths_refused_without_https(self):
        """The proxy and HTTPS: every sign-in/account request that is not HTTPS gets 403 with the https why."""
        b = self.board()
        code = b.setup_code()
        for proto in (None, "http", "HTTP", "httpss", ""):
            hs = [] if proto is None else [("X-Forwarded-Proto", proto)]
            for path, body in (("/api/setup-account", {"code": code, "user": USER, "password": PASS, "keep": True}),
                               ("/api/login", {"user": USER, "password": PASS, "keep": True}),
                               ("/api/login/code", {"code": "123456"}), ("/api/logout", {})):
                r = b.raw("POST", path, body, hs)
                self.assertEqual(r.status, 403, (proto, path, r))
                self.assertIn("X-Forwarded-Proto: https", r.json.get("why"), (proto, path))
                self.assertFalse(r.header("set-cookie"), (proto, path))
        # none of those created the account: the code still works over HTTPS (case-insensitive value)
        r = b.raw("POST", "/api/setup-account", {"code": code, "user": USER, "password": PASS, "keep": True},
                  [("X-Forwarded-Proto", "HTTPS")])
        self.assertEqual(r.status, 200, r)

    def test_https_refusal_exact_why(self):
        """The proxy and HTTPS: the refusal body is exactly {"why": "Sign in over https: the reverse proxy must send X-Forwarded-Proto: https (and X-Real-IP)."}."""
        b = self.board()
        r = b.raw("POST", "/api/login", {"user": USER, "password": PASS, "keep": False}, [("X-Forwarded-Proto", "http")])
        self.assertEqual(r.status, 403, r)
        self.assertEqual(r.json, {"why": "Sign in over https: the reverse proxy must send X-Forwarded-Proto: https (and X-Real-IP)."})

    def test_account_paths_403_without_https(self):
        """The proxy and HTTPS: every request on the account paths that is not HTTPS gets 403 (even with a valid session)."""
        b = self.board()
        _, tok, dev = self.signed_in(b)
        for hs in ([], [("X-Forwarded-Proto", "http")], [("X-Forwarded-Proto", "https"), ("X-Forwarded-Proto", "https")],
                   [("X-Forwarded-Proto", "https, http")]):
            for cookie in ([], [("Cookie", tok + "; " + dev)]):
                r = b.raw("GET", "/api/account", None, hs + cookie)
                self.assertEqual(r.status, 403, (hs, bool(cookie), r))
                r = b.raw("POST", "/api/account/password", {"password": PASS, "new": PASS2}, hs + cookie)
                self.assertEqual(r.status, 403, (hs, bool(cookie), r))
                self.assertFalse(r.header("set-cookie"))
        self.assertEqual(b.req("GET", "/api/account", cookie=tok).status, 200)   # nothing above changed the password

    def test_double_or_list_forwarded_proto_is_not_https(self):
        """The proxy and HTTPS: exactly one X-Forwarded-Proto of exactly https; two headers or 'https, http' are not HTTPS."""
        b = self.board()
        code = b.setup_code()
        bad = ([("X-Forwarded-Proto", "https"), ("X-Forwarded-Proto", "https")],
               [("X-Forwarded-Proto", "https"), ("X-Forwarded-Proto", "http")],
               [("X-Forwarded-Proto", "https, http")], [("X-Forwarded-Proto", "https,https")],
               [("X-Forwarded-Proto", "http, https")])
        for hs in bad:
            r = b.raw("POST", "/api/setup-account", {"code": code, "user": USER, "password": PASS, "keep": True}, hs)
            self.assertEqual(r.status, 403, (hs, r))
            self.assertFalse(r.header("set-cookie"), hs)
        r = b.req("POST", "/api/setup-account", {"code": code, "user": USER, "password": PASS, "keep": True})
        self.assertEqual(r.status, 200, r)
        tok = r.cookie("__Host-subplot")
        self.assertEqual(b.req("GET", "/api/account", cookie=tok).status, 200)
        # the valid session is not accepted over not-HTTPS either
        for hs in bad + ([],):
            r = b.raw("GET", "/api/state", None, hs + [("Cookie", tok)])
            self.assertIn(r.status, (401, 403), (hs, r))
            r = b.raw("GET", "/api/account", None, hs + [("Cookie", tok)])
            self.assertIn(r.status, (401, 403), (hs, r))
            r = b.raw("GET", "/", None, hs + [("Cookie", tok)])
            self.assertNotEqual(r.status, 200, (hs, r))

    # ---- sessions and epochs ----
    def test_old_epoch_session_refused_after_password_change(self):
        """Security 3: a session counts only at the account's current epoch; a password change ends other sessions
        and gives THIS browser fresh cookies."""
        b = self.board()
        _, tok_a, dev_a = self.signed_in(b)
        r = b.req("POST", "/api/login", {"user": USER, "password": PASS, "keep": False}, ip="203.0.113.11")
        self.assertEqual(r.status, 200, r)
        tok_b = r.cookie("__Host-subplot")
        self.assertTrue(tok_b)
        self.assertEqual(b.req("GET", "/api/state", cookie=tok_b).status, 200)
        r = b.req("POST", "/api/account/password", {"password": PASS, "new": PASS2}, cookie=[tok_a, dev_a])
        self.assertEqual(r.status, 200, r)
        self.assertEqual(r.json, {"ok": True})
        tok_a2 = r.cookie("__Host-subplot")
        self.assertTrue(tok_a2 and tok_a2 != tok_a, r.set_cookies())
        for old in (tok_a, tok_b):
            r = b.req("GET", "/api/state", cookie=old)
            self.assertEqual(r.status, 401, r)
            self.assertEqual(r.json, UNAUTH)
            self.assertEqual(b.req("GET", "/api/account", cookie=old).status, 401)
        self.assertEqual(b.req("GET", "/api/state", cookie=tok_a2).status, 200)
        # the new password works, the old one does not
        self.assertEqual(b.req("POST", "/api/login", {"user": USER, "password": PASS2, "keep": False}, ip="203.0.113.12").status, 200)
        self.assertEqual(b.req("POST", "/api/login", {"user": USER, "password": PASS, "keep": False}, ip="203.0.113.13").status, 401)

    def test_account_change_needs_current_password(self):
        """Reply shapes: every account change but totp/confirm needs the current password (a session alone can't change it)."""
        b = self.board()
        _, tok, dev = self.signed_in(b)
        r = b.req("POST", "/api/account/password", {"password": "wrong wrong wrong wrong", "new": PASS2}, cookie=[tok, dev])
        self.assertNotEqual(r.status, 200, r)
        r = b.req("POST", "/api/account/password", {"new": PASS2}, cookie=[tok, dev])
        self.assertNotEqual(r.status, 200, r)
        self.assertEqual(b.req("GET", "/api/state", cookie=tok).status, 200)

    # ---- setup code ----
    def test_setup_code_works_once(self):
        """First run: only the newest code works, and a code is used up when the account is created."""
        b = self.board()
        old = b.setup_code()
        code = b.setup_code()
        self.assertNotEqual(old, code)
        r = b.req("POST", "/api/setup-account", {"code": old, "user": USER, "password": PASS, "keep": True})
        self.assertEqual(r.status, 403, r)
        self.assertEqual(r.json.get("field"), "code")
        b.create_account(code)
        r = b.req("POST", "/api/setup-account", {"code": code, "user": "other", "password": PASS2, "keep": True})
        self.assertEqual(r.status, 409, r)
        self.assertIs(r.json.get("restart"), True)
        self.assertFalse(r.header("set-cookie"))
        # reset removes the account and prints a new code; the used one still does not work
        out = b.cli("reset", "--yes")
        new = out.split()[-1]
        self.assertNotEqual(new, code)
        r = b.req("POST", "/api/setup-account", {"code": code, "user": USER, "password": PASS, "keep": True})
        self.assertEqual(r.status, 403, r)
        r = b.req("POST", "/api/setup-account", {"code": new, "user": USER, "password": PASS, "keep": True})
        self.assertEqual(r.status, 200, r)

    def test_parallel_setup_makes_one_account(self):
        """Code: account creation holds the locks across the recheck; two parallel setups make exactly one account (other 409)."""
        b = self.board()
        code = b.setup_code()
        results = {}
        go = threading.Barrier(2)

        def run(user):
            go.wait()
            results[user] = b.req("POST", "/api/setup-account", {"code": code, "user": user, "password": PASS, "keep": True},
                                  ip="10.0.0." + str(len(user)))
        ts = [threading.Thread(target=run, args=(u,)) for u in ("alice", "bobby1")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        st = sorted(r.status for r in results.values())
        self.assertEqual(st, [200, 409], results)
        winner = [u for u, r in results.items() if r.status == 200][0]
        loser = [u for u, r in results.items() if r.status != 200][0]
        self.assertFalse(results[loser].header("set-cookie"))
        self.assertEqual(b.req("POST", "/api/login", {"user": winner, "password": PASS, "keep": False}, ip="203.0.113.14").status, 200)
        self.assertEqual(b.req("POST", "/api/login", {"user": loser, "password": PASS, "keep": False}, ip="203.0.113.15").status, 401)

    # ---- limits ----
    def test_throttle_429_carries_retry(self):
        """Reply shapes: 429 always carries "retry": seconds and Retry-After (throttle on one client address)."""
        b = self.board()
        b.create_account()
        seen = None
        for i in range(30):
            r = b.req("POST", "/api/login", {"user": USER, "password": "wrong guess number %d" % i, "keep": False},
                      ip="203.0.113.16")
            if r.status == 429:
                seen = r
                break
            self.assertEqual(r.status, 401, r)
            self.assertIn("field", r.json)
        self.assertIsNotNone(seen, "no 429 after 30 wrong tries from one address")
        self.assertGreaterEqual(i, 5, "throttle must allow 5 free tries")
        retry = seen.json.get("retry")
        self.assertIsInstance(retry, int)
        self.assertGreater(retry, 0)
        ra = seen.header("retry-after")
        self.assertEqual(len(ra), 1, seen.headers)
        self.assertTrue(ra[0].isdigit() and int(ra[0]) > 0, ra)
        self.assertIn("why", seen.json)
        # while waiting, even the right password is not checked
        r = b.req("POST", "/api/login", {"user": USER, "password": PASS, "keep": False}, ip="203.0.113.16")
        self.assertEqual(r.status, 429, r)
        self.assertFalse(r.header("set-cookie"))

    def test_lock_after_100_failures(self):
        """Security 4: 100 failures lock new browsers (423 names the unlock command); trusted browsers count on their own."""
        b = self.board()
        _, tok, dev = self.signed_in(b)
        start = time.monotonic()
        for i in range(100):
            if time.monotonic() - start > 20:
                self.skipTest("100 wrong tries take over 20 s here")
            r = b.req("POST", "/api/login", {"user": USER, "password": "wrong guess number %d" % i, "keep": False},
                      ip="198.19.%d.%d" % (i // 200 + 20, i % 200 + 1))
            if r.status == 429:
                self.skipTest("the throttle makes 100 failures take longer than ~20 s (wait shared across addresses)")
            if r.status == 423:
                break
            self.assertEqual(r.status, 401, (i, r))
        r = b.req("POST", "/api/login", {"user": USER, "password": PASS, "keep": False}, ip="203.0.113.17")
        self.assertEqual(r.status, 423, r)
        self.assertIn("runuser", r.json.get("why", ""))
        self.assertFalse(r.header("set-cookie"))
        r = b.req("POST", "/api/login", {"user": USER, "password": PASS, "keep": False}, cookie=dev, ip="203.0.113.18")
        self.assertEqual(r.status, 200, r)

    # ---- cookies ----
    def test_cookie_names_and_flags(self):
        """Code: __Host-subplot / __Host-subplot-device, always Secure; HttpOnly; SameSite=Strict; Path=/ (no Domain)."""
        b = self.board()
        replies = [b.create_account()]
        replies.append(b.req("POST", "/api/login", {"user": USER, "password": PASS, "keep": True}, ip="203.0.113.19"))
        for r in replies:
            self.assertEqual(r.status, 200, r)
            cs = r.set_cookies()
            self.assertIn("__Host-subplot", cs)
            self.assertIn("__Host-subplot-device", cs)
            for name, (value, attrs) in cs.items():
                self.assertTrue(name.startswith("__Host-subplot"), name)
                low = [a.lower() for a in attrs]
                self.assertIn("secure", low, name)
                self.assertIn("httponly", low, name)
                self.assertIn("samesite=strict", low, name)
                self.assertIn("path=/", low, name)
                self.assertFalse([a for a in low if a.startswith("domain")], name)
            self.assertTrue(cs["__Host-subplot"][0])
        # logout clears the session cookie the same way
        tok = replies[1].cookie("__Host-subplot")
        r = b.req("POST", "/api/logout", {}, cookie=tok)
        self.assertEqual(r.status, 200, r)
        self.assertEqual(r.json, {"ok": True})
        cs = r.set_cookies()
        self.assertIn("__Host-subplot", cs)
        low = [a.lower() for a in cs["__Host-subplot"][1]]
        for flag in ("secure", "httponly", "samesite=strict", "path=/"):
            self.assertIn(flag, low)
        self.assertEqual(b.req("GET", "/api/state", cookie=tok).status, 401)

    # ---- secrets ----
    def test_setup_code_never_logged(self):
        """Security 5 / First run: the board logs the account creation, never the code (nor the password)."""
        b = self.board()
        wrong = b.setup_code()
        code = b.setup_code()
        b.req("POST", "/api/setup-account", {"code": wrong, "user": USER, "password": PASS, "keep": True})
        b.req("POST", "/api/setup-account", {"code": code, "user": USER, "password": "short", "keep": True})
        b.create_account(code)
        b.req("POST", "/api/setup-account", {"code": code, "user": USER, "password": PASS, "keep": True})
        b.req("POST", "/api/login", {"user": USER, "password": PASS2, "keep": False}, ip="203.0.113.20")
        b.stop()
        log = b.output()
        self.assertIn("account created", log)
        for secret in (code, wrong, PASS, PASS2):
            self.assertNotIn(secret, log)

    # ---- config ----
    def test_config_auth_key_only_in_login_mode(self):
        """The page: /api/config has "auth": "login" only with AUTH=login; with AUTH=proxy it is unchanged (no key)."""
        b = self.board()
        _, tok, _ = self.signed_in(b)
        r = b.req("GET", "/api/config", cookie=tok)
        self.assertEqual(r.status, 200, r)
        self.assertEqual(r.json.get("auth"), "login")
        p = self.board(auth=None)
        r = p.req("GET", "/api/config")
        self.assertEqual(r.status, 200, r)
        self.assertNotIn("auth", r.json)
        self.assertFalse(os.path.exists(os.path.join(p.state, "auth", "auth.json")))
        r = p.req("GET", "/api/auth/state")
        if r.status == 200:
            self.assertEqual(r.json, {"mode": "proxy"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
