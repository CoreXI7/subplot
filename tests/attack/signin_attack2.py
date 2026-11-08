#!/usr/bin/env python3
"""Break-it runner, round 2 (2026-11-08, the owner: "do some more tests on the login page"): path and method tricks, the session
lifecycle, the code-step cookie, account-name timing, fake proxy headers, oversized headers, idle connections, body
type confusion and headers on error replies. Same harness as signin_attack.py.

Starts a real board on a unix socket under SUBS_SANDBOX, sends raw HTTP with chosen headers
(that is how HTTPS and the client address are injected), and prints PASS/FAIL per case."""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SANDBOX = os.environ["SUBS_SANDBOX"]
BASE = tempfile.mkdtemp(prefix="signin-attack-", dir=SANDBOX)
STATE = os.path.join(BASE, "state")
os.makedirs(os.path.join(STATE, "auth"), 0o700)
os.chmod(os.path.join(STATE, "auth"), 0o700)
RULES = os.path.join(BASE, "rules.json")
with open(RULES, "w") as f:
    f.write("{}")
SOCK = os.path.join(BASE, "b.sock")
HOST = "subs.example"
ORIGIN = "https://" + HOST
ENV = {**os.environ, "SUBS_STATE": STATE, "SUBS_SOCK": SOCK, "SUBS_HOST": HOST, "SUBS_AUTH": "login",
       "SUBS_RULES": RULES, "SUBS_SOCK_GROUP": "", "SUBS_HOME": BASE}
os.environ["SUBS_STATE"] = STATE
sys.path.insert(0, os.path.join(ROOT, "subplot"))
import auth  # noqa: E402  (only totp_code / _b32 are used)

USER = "owner"
PW1 = "violet-anchor-pebble-sunrise-48"
PW2 = "marble-cactus-lantern-orbit-93"

results = []
replies = []          # (path, body bytes) of every reply, for the secret scan
LOG = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else ": " + str(detail)[:200]))


_ip = [10]


def new_ip():
    _ip[0] += 1
    return f"198.18.{_ip[0] // 250}.{_ip[0] % 250 + 1}"


def raw(data, timeout=20):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall(data)
        out = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            out += c
        return out
    except socket.timeout:
        return b"TIMEOUT"
    except OSError as exc:
        return repr(exc).encode()
    finally:
        s.close()


def req(method, path, body=None, cookie=None, ip="auto", proto=("https",), headers=None, csrf=True, timeout=20):
    """Returns (status, json-or-bytes, {cookie name: value}). headers: list of (name, value) added verbatim."""
    b = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    h = [("Host", HOST)]
    for p in proto:
        h.append(("X-Forwarded-Proto", p))
    if ip == "auto":
        ip = new_ip()
    if ip is not None:
        h.append(("X-Real-IP", ip))
    if cookie:
        h.append(("Cookie", cookie))
    if method == "POST":
        if csrf:
            h.append(("X-Subs", "1"))
        h += [("Content-Type", "application/json"), ("Content-Length", str(len(b)))]
    h += headers or []
    head = f"{method} {path} HTTP/1.1\r\n".encode() + b"".join(
        (k + ": ").encode() + (v.encode("utf-8") if isinstance(v, str) else v) + b"\r\n" for k, v in h)
    r = raw(head + b"Connection: close\r\n\r\n" + b, timeout=timeout)
    if r == b"TIMEOUT":
        return -2, r, {}
    top, _, data = r.partition(b"\r\n\r\n")
    lines = top.decode("latin-1").split("\r\n")
    try:
        st = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return -1, r[:120], {}
    ck = {}
    for l in lines[1:]:
        if l.lower().startswith("set-cookie:"):
            nv = l.split(":", 1)[1].strip().split(";")[0]
            n, _, v = nv.partition("=")
            ck[n] = v
    replies.append((path, top + b"\r\n\r\n" + data))
    try:
        d = json.loads(data)
    except Exception:
        d = data[:120]
    return st, d, ck


def sess(cks):
    return "__Host-subplot=" + cks["__Host-subplot"]


def why(d):
    return d.get("why") if isinstance(d, dict) else d


# ---- start the board
code = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "subplot", "auth.py"), "setup-code"],
                      env=ENV, capture_output=True, text=True, cwd=ROOT).stdout.split()[-1]
srv = subprocess.Popen([sys.executable, "-I", "subplot/server.py"], env=ENV, cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
threading.Thread(target=lambda: [LOG.append(l) for l in srv.stdout], daemon=True).start()
for _ in range(100):
    if os.path.exists(SOCK):
        break
    time.sleep(0.1)
DEADLINE = time.time() + 110

try:
    st, d, ck = req("POST", "/api/setup-account", {"code": code, "user": USER, "password": PW1, "keep": False})
    check("setup-account-200", st == 200 and "__Host-subplot" in ck, (st, d, list(ck)))
    S = sess(ck)
    TOK = S.split("=", 1)[1]
    check("session-works", req("GET", "/api/account", cookie=S)[0] == 200)

    def login(pw=PW1, ip="auto", cookie=None):
        for _ in range(4):
            st, d, ck = req("POST", "/api/login", {"user": USER, "password": pw, "keep": False}, ip=ip, cookie=cookie)
            if st != 429 or not isinstance(d, dict) or d.get("retry", 99) > 10:
                return st, d, ck
            time.sleep(d["retry"] + 0.2)
        return st, d, ck

    def leaked(d):
        return isinstance(d, dict) and ("sessions" in d or "user" in d or "items" in d)

    # ---- A. account-name timing (first: few failures counted so far). The only account is "owner".
    def timed(user):
        t = time.perf_counter()
        st, _, _ = req("POST", "/api/login", {"user": user, "password": "wrong-password-timing-1", "keep": False})
        return time.perf_counter() - t, st
    known, unknown, sts = [], [], []
    for i in range(12):   # alternate, spaced out so the per-try pacing never answers first
        for user, bucket in ((USER, known), ("nobody-here", unknown)):
            dt, st = timed(user)
            sts.append(st)
            if st == 401:
                bucket.append(dt)
        time.sleep(0.3)
    known.sort(); unknown.sort()
    print(f"  note: timing samples {len(known)} + {len(unknown)}")
    mk = known[len(known) // 2] if known else 0
    mu = unknown[len(unknown) // 2] if unknown else 0
    print(f"  note: wrong password median {mk*1000:.0f} ms, wrong name median {mu*1000:.0f} ms; replies {sorted(set(sts))}")
    check("timing-name-not-revealed", known and unknown and min(mk, mu) / max(mk, mu) > 0.6, (round(mk, 3), round(mu, 3)))
    st1, d1, _ = req("POST", "/api/login", {"user": USER, "password": "wrong-password-timing-2", "keep": False})
    st2, d2, _ = req("POST", "/api/login", {"user": "nobody-here", "password": "wrong-password-timing-2", "keep": False})
    check("wrong-name-same-reply", (st1, why(d1)) == (st2, why(d2)), ((st1, why(d1)), (st2, why(d2))))

    # ---- B. path and method tricks: no account or state data without a session
    paths = ["/api/account/", "/API/account", "/api/Account", "/api//account", "//api/account", "/api/./account",
             "/api/%61ccount", "/login/../api/account", "/login%2f..%2fapi%2faccount", "/api/account;/login",
             "/api/account?/login", "/api/account#", "/login.js/../api/account", "/api/state/", "/api/state;x",
             "/api/auth/state/../../api/state", "/api/account%00", "/api/account\t", "/./api/account", "/api/account/."]
    for p in paths:
        st, d, _ = req("GET", p)
        check("path-get " + repr(p), st in (400, 401, 404, 303) and not leaked(d), (st, d))
    for p in ["/api/decision/", "/API/decision", "/api//decision", "/api/login/../decision", "/api/logout/../pc"]:
        st, d, _ = req("POST", p, {"key": "ep-1", "decision": "keep"})
        check("path-post " + p, st in (400, 401, 403, 404), (st, d))
    for m in ["HEAD", "OPTIONS", "PUT", "DELETE", "PATCH", "TRACE", "CONNECT", "PROPFIND", "get", "GeT"]:
        st, d, _ = req(m, "/api/account")
        check("method " + m, st in (400, 401, 405, 501) and not leaked(d), (st, d))
    for line in [b"GET https://subs.example/api/account HTTP/1.1", b"GET /api/account HTTP/1.0", b"GET /api/account",
                 b"GET /api/account HTTP/2.0", b"GET  /api/account HTTP/1.1", b"GET /api/account HTTP/1.1 extra"]:
        r = raw(line + b"\r\nHost: subs.example\r\nX-Forwarded-Proto: https\r\nConnection: close\r\n\r\n")
        check("request-line " + line.decode()[:45], b'"sessions"' not in r and r != b"TIMEOUT", r[:60])

    # ---- C. proxy sign-in headers mean nothing in login mode
    for h in [("Remote-User", USER), ("Remote-Groups", "admins"), ("X-Forwarded-User", USER), ("X-Remote-User", USER),
              ("Authorization", "Basic b3duZXI6eA=="), ("X-Authentik-Username", USER), ("X-Auth-Request-User", USER)]:
        st, d, _ = req("GET", "/api/account", headers=[h])
        check("fake-proxy-header " + h[0], st == 401 and not leaked(d), (st, d))
        st, d, _ = req("GET", "/api/state", headers=[h])
        check("fake-proxy-header-state " + h[0], st == 401, (st, d))

    # ---- D. session lifecycle
    st, d, ck = login()
    check("login-200", st == 200 and ck.get("__Host-subplot"), (st, d))
    L = sess(ck)
    st, d, ck = req("POST", "/api/logout", {}, cookie=L)
    check("logout-200", st == 200, (st, d))
    check("logout-clears-cookie", ck.get("__Host-subplot", "x") == "", ck)
    check("logout-old-cookie-dead", req("GET", "/api/account", cookie=L)[0] == 401)
    # fixation: a planted well-formed token is not adopted and not echoed back
    planted = "A" * len(TOK)
    st, d, ck = login(cookie="__Host-subplot=" + planted)
    check("fixation-new-token", st == 200 and ck.get("__Host-subplot") not in (None, planted), (st, ck))
    check("fixation-planted-dead", req("GET", "/api/account", cookie="__Host-subplot=" + planted)[0] == 401)
    F = sess(ck)
    # sign out everywhere ends the other session
    st, d, ck2 = login()
    O = sess(ck2)
    st, d, ckx = req("POST", "/api/account/sessions/end", {"password": PW1, "all": True}, cookie=F)
    check("sessions-end-all-200", st == 200, (st, d))
    check("sessions-end-all-other-dead", req("GET", "/api/account", cookie=O)[0] == 401)
    st_s, _, _ = req("GET", "/api/account", cookie=S)
    check("sessions-end-all-first-dead", st_s == 401, st_s)
    # a session alone can't change anything: wrong password on an account change, 10 tries sign it out
    st, d, ck = login()
    W = sess(ck)
    codes, waited_s, i = [], 0, 0
    while len([c for c in codes if c == 403]) < 10 and waited_s < 180 and i < 40:
        st, d, _ = req("POST", "/api/account/password", {"password": f"wrong-{i:02d}-abcdefgh", "new": "quartz-meadow-falcon-river-27"}, cookie=W)
        codes.append(st); i += 1
        if st == 429 and isinstance(d, dict):
            time.sleep(min(d.get("retry", 1), 60) + 0.2); waited_s += min(d.get("retry", 1), 60)
        elif st == 401:
            break
    print(f"  note: re-confirm replies {codes} (waited {waited_s:.0f} s)")
    check("reconfirm-wrong-refused", all(c in (401, 403, 429) for c in codes), codes)
    check("reconfirm-10-signs-out", req("GET", "/api/account", cookie=W)[0] == 401, codes)
    # a valid session over plain http (no X-Forwarded-Proto) is not accepted
    st, d, ck = login()
    S = sess(ck)
    st, d, _ = req("GET", "/api/account", cookie="subplot=" + S.split("=", 1)[1], proto=())
    check("session-over-http-refused", st in (401, 403) and not leaked(d), (st, d))
    st, d, _ = req("GET", "/api/state", cookie="subplot=" + S.split("=", 1)[1], proto=())
    check("state-over-http-refused", st in (401, 403), (st, d))

    # ---- E. the code step: the -pre cookie is not a session
    st, d, _ = req("POST", "/api/account/totp/start", {"password": PW1}, cookie=S)
    key = auth._b32(d["secret"]) if st == 200 else None
    st, d, ck = req("POST", "/api/account/totp/confirm", {"code": auth.totp_code(key, int(time.time()) // 30)}, cookie=S)
    check("totp-on", st == 200 and ck.get("__Host-subplot"), (st, d))
    S = sess(ck)
    PIP = new_ip()
    st, d, ck = login(ip=PIP)
    pre = ck.get("__Host-subplot-pre")
    check("code-due", st == 200 and isinstance(d, dict) and d.get("code_due") and pre and not ck.get("__Host-subplot"), (st, d, ck))
    if pre:
        for c in ["__Host-subplot=" + pre, "__Host-subplot-pre=" + pre]:
            st, d, _ = req("GET", "/api/account", cookie=c, ip=PIP)
            check("pre-cookie-not-session " + c.split("=")[0], st == 401 and not leaked(d), (st, d))
            st, d, _ = req("POST", "/api/decision", {"key": "ep-1", "decision": "keep"}, cookie=c, ip=PIP)
            check("pre-cookie-cannot-decide " + c.split("=")[0], st == 401, (st, d))
        tries = [req("POST", "/api/login/code", {"code": f"{i:06d}"}, cookie="__Host-subplot-pre=" + pre, ip=PIP)
                 for i in range(1, 7)]
        check("code-step-wrong-refused", all(t[0] in (401, 429) and not t[2].get("__Host-subplot") for t in tries),
              [t[:2] for t in tries])
        good = auth.totp_code(key, int(time.time()) // 30)
        st, d, ck = req("POST", "/api/login/code", {"code": good}, cookie="__Host-subplot-pre=" + pre, ip=PIP)
        check("code-step-dead-after-5-wrong", st in (401, 429) and not ck.get("__Host-subplot"), (st, d))
    st, d, ck = req("POST", "/api/login/code", {"code": "123456"}, ip=PIP)
    check("code-without-pre-refused", st in (401, 429) and not ck.get("__Host-subplot"), (st, d))

    # ---- F. body type confusion: clean 4xx, never 5xx, never a session
    bodies = [b"[]", b"null", b'"x"', b"1", b"{}", b'{"user": ["owner"], "password": "x"}',
              b'{"user": "owner", "password": {"$ne": 1}}', b'{"user": "owner", "password": true}',
              b'{"user": "owner", "password": 0}', b'{"user": "own\\u0000er", "password": "x"}',
              b'{"user": "owner", "user": "x", "password": "p"}', b"{" * 3000, b'{"user": "owner", "password": "' + b"a" * 8000 + b'"}',
              b"\xff\xfe{}", b'{"user": "owner", "password": NaN}']
    for b in bodies:
        st, d, ck = req("POST", "/api/login", b)
        check("body " + b[:40].decode("latin-1"), 400 <= st < 500 and not ck.get("__Host-subplot"), (st, d))
    for b in [b"[]", b"null", b'{"password": ["x"]}', b'{"password": "' + PW1.encode() + b'", "all": "yes"}']:
        st, d, _ = req("POST", "/api/account/sessions/end", b, cookie=S)
        check("account-body " + b[:30].decode(), 400 <= st < 500, (st, d))
    check("alive-after-bodies", req("GET", "/api/account", cookie=S)[0] == 200)

    # ---- G. oversized and odd headers
    for name, hdrs in {"cookie-70k": [("Cookie", "a=" + "b" * 70000)], "200-headers": [(f"X-H{i}", "v") for i in range(200)],
                       "header-8k": [("X-Big", "z" * 8000)], "host-dup": [("Host", "evil.example")],
                       "cl-and-te": [("Transfer-Encoding", "chunked")]}.items():
        st, d, _ = req("GET", "/api/account", cookie=S if name != "cookie-70k" else None, headers=hdrs)
        check("headers " + name, st == -1 or 400 <= st < 500 or (st == 200 and name in ("header-8k",)), (st, d))
    check("alive-after-headers", req("GET", "/api/auth/state")[0] == 200)

    # ---- H. every refusal still carries the security headers
    must = [b"content-security-policy", b"cache-control: no-store", b"x-content-type-options: nosniff"]
    samples = {"401": ("GET", "/api/account", None), "303": ("GET", "/", None), "405": ("PUT", "/api/account", None),
               "404": ("GET", "/nope", None), "400": ("GET", "/a/../b", None)}
    for label, (m, p, c) in samples.items():
        h = [("Host", HOST), ("X-Forwarded-Proto", "https"), ("X-Real-IP", new_ip())]
        r = raw(f"{m} {p} HTTP/1.1\r\n".encode() + b"".join(f"{k}: {v}\r\n".encode() for k, v in h) + b"Connection: close\r\n\r\n")
        top = r.partition(b"\r\n\r\n")[0].lower()
        check("headers-on-" + label, all(x in top for x in must), top[:200])
    st, d, _ = req("GET", "/")
    r = [x for p, x in replies if p == "/"][-1]
    check("redirect-is-relative", b"\r\nLocation: login\r\n" in r, r[:200])

    # ---- I. idle connections (single-threaded board, 15 s per connection)
    idle = []
    for _ in range(3):
        s_ = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s_.connect(SOCK)
        idle.append(s_)
    t = time.perf_counter()
    st, d, _ = req("GET", "/api/auth/state", timeout=90)
    waited = time.perf_counter() - t
    for s_ in idle:
        s_.close()
    print(f"  note: with 3 idle connections open, a normal request waited {waited:.1f} s")
    check("idle-connections-dont-block", st == 200 and waited < 3, (st, round(waited, 1)))
    # a client trickling its request one byte at a time is cut at the read deadline, not kept for hours
    tr = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    tr.connect(SOCK)
    t0, cut = time.perf_counter(), None
    msg = b"POST /api/login HTTP/1.1\r\nHost: subs.example\r\nX-Forwarded-Proto: https\r\nX-Subs: 1\r\nContent-Length: 4000\r\n\r\n"
    try:
        for i in range(400):
            tr.sendall(msg[i:i + 1] if i < len(msg) else b"a")
            time.sleep(0.1)
            if time.perf_counter() - t0 > 30:
                break
    except OSError:
        cut = time.perf_counter() - t0
    tr.close()
    print(f"  note: trickling client cut after {cut if cut is None else round(cut, 1)} s")
    check("trickle-cut-at-deadline", cut is not None and cut < 15, cut)
    # 20 idle connections (more than the cap): a real request still gets in
    many = []
    for _ in range(20):
        s_ = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s_.connect(SOCK)
        many.append(s_)
    t = time.perf_counter()
    st, d, _ = req("GET", "/api/auth/state", timeout=30)
    w2 = time.perf_counter() - t
    for s_ in many:
        s_.close()
    print(f"  note: with 20 idle connections, a normal request waited {w2:.1f} s")
    check("idle-flood-still-answers", st == 200 and w2 < 3, (st, round(w2, 1)))

    # ---- J. secrets never in the log
    check("within-time", time.time() < DEADLINE + 120, "slow")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()
    time.sleep(0.2)

log = "".join(LOG)
check("log-no-secrets", not any(s in log for s in [code, PW1, PW2]), "a secret is in the board's output")
shutil.rmtree(BASE, ignore_errors=True)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
