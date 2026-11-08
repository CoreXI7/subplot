#!/usr/bin/env python3
"""Break-it runner for the board's own sign-in (AUTH=login), written blind from .crew/SPEC.md.

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
    check("session-works", req("GET", "/api/account", cookie=S)[0] == 200)

    # ---- 6. old session refused after a password change
    st, d, ck = req("POST", "/api/account/password", {"password": PW1, "new": PW2}, cookie=S)
    check("pw-change-200", st == 200 and "__Host-subplot" in ck, (st, d))
    OLD, S = S, sess(ck) if "__Host-subplot" in ck else S
    check("pw-change-old-session-refused", req("GET", "/api/account", cookie=OLD)[0] == 401)
    check("pw-change-new-session-ok", req("GET", "/api/account", cookie=S)[0] == 200)
    check("pw-change-old-password-refused",
          req("POST", "/api/login", {"user": USER, "password": PW1, "keep": False})[0] in (401,))
    TOK = S.split("=", 1)[1]

    # ---- 4. TOTP reuse and recovery reuse
    st, d, _ = req("POST", "/api/account/totp/start", {"password": PW2}, cookie=S)
    check("totp-start-200", st == 200 and isinstance(d, dict) and "secret" in d, (st, d))
    SECRET = d["secret"]
    key = auth._b32(SECRET)
    s0 = int(time.time()) // 30
    st, d, ck = req("POST", "/api/account/totp/confirm", {"code": auth.totp_code(key, s0)}, cookie=S)
    check("totp-confirm-200", st == 200 and len(d.get("recovery", [])) == 10 and "__Host-subplot" in ck, (st, d))
    RC = d["recovery"]
    S = sess(ck)
    c1 = auth.totp_code(key, s0 + 1)

    def code_step(code_, label):
        ip = new_ip()
        for _ in range(4):
            st, d, ck = req("POST", "/api/login", {"user": USER, "password": PW2, "keep": False}, ip=ip)
            if st != 429 or not isinstance(d, dict) or d.get("retry", 99) > 10:
                break
            time.sleep(d["retry"] + 0.2)
        if not (st == 200 and isinstance(d, dict) and d.get("code_due") and ck.get("__Host-subplot-pre")):
            check(label + "-code-due", False, (st, d))
            return None
        return req("POST", "/api/login/code", {"code": code_}, cookie="__Host-subplot-pre=" + ck["__Host-subplot-pre"],
                   ip=ip)

    r = code_step(auth.totp_code(key, s0), "totp-confirm-code-reuse")
    if r:
        check("totp-confirm-code-reuse-refused", r[0] == 401 and "__Host-subplot" not in r[2], r[:2])
    r = code_step(c1, "totp-first-use")
    if r:
        check("totp-first-use-ok", r[0] == 200 and r[2].get("__Host-subplot"), r[:2])
    r = code_step(c1, "totp-reuse")
    if r:
        check("totp-reuse-refused", r[0] == 401 and not r[2].get("__Host-subplot"), r[:2])
    r = code_step(RC[0], "recovery-first-use")
    if r:
        check("recovery-first-use-ok", r[0] == 200 and r[2].get("__Host-subplot"), r[:2])
    r = code_step(RC[0], "recovery-reuse")
    if r:
        check("recovery-reuse-refused", r[0] == 401 and not r[2].get("__Host-subplot"), r[:2])
    r = code_step(RC[0].upper() if RC[0] != RC[0].upper() else RC[0].lower(), "recovery-reuse-case")
    if r:
        check("recovery-reuse-other-case-refused", r[0] == 401 and not r[2].get("__Host-subplot"), r[:2])
    st, d, _ = req("GET", "/api/account", cookie=S)
    check("account-recovery-left-9", st == 200 and d.get("recovery_left") == 9, (st, d if st != 200 else d.get("recovery_left")))

    # ---- 1. cookie floods and junk
    flood = "; ".join(f"__Host-subplot=junk{i:04d}" for i in range(400)) + "; " + S
    st, d, _ = req("GET", "/api/account", cookie=flood)
    check("cookie-flood-clean", st in (200, 400, 401, 431), (st, d))
    check("cookie-flood-no-5xx", st != -2 and not 500 <= st < 600, st)
    malformed = {
        "no-equals": "__Host-subplot",
        "no-equals-junk": "garbage;;;;;",
        "10kb-value": "__Host-subplot=" + "A" * 10240,
        "non-ascii": "__Host-subplot=tökén☃",
        "dup-names": "__Host-subplot=aaaa; __Host-subplot=bbbb",
        "empty-value": "__Host-subplot=",
        "quoted": '__Host-subplot="' + TOK + '"x',
        "token-plus-junk": "__Host-subplot=" + TOK + "x",
    }
    for name, c in malformed.items():
        st, d, _ = req("GET", "/api/account", cookie=c)
        check("cookie-" + name + "-no-session", st in (400, 401, 431), (st, d))
    st, d, _ = req("GET", "/api/account", cookie="subplot=" + TOK)
    check("cookie-no-host-prefix-refused", st == 401, (st, d))
    st, d, _ = req("GET", "/api/account", cookie="__host-subplot=" + TOK)
    check("cookie-lowercase-prefix-refused", st == 401, (st, d))
    check("board-alive-after-cookies", req("GET", "/api/auth/state")[0] == 200)

    # ---- 2. header smuggling
    bad_protos = {"two-headers": ("https", "https"), "https-http": ("https, http",), "http": ("http",),
                  "absent": (), "two-mixed": ("https", "http")}
    for name, p in bad_protos.items():
        st, d, ck = req("POST", "/api/login", {"user": USER, "password": PW2, "keep": False}, proto=p)
        check("proto-" + name + "-login-refused", st == 403 and not ck.get("__Host-subplot"), (st, d))
        st, d, _ = req("GET", "/api/account", cookie=S, proto=p)
        check("proto-" + name + "-session-refused", st in (401, 403), (st, d))
        st, d, _ = req("GET", "/api/state", cookie=S, proto=p)
        check("proto-" + name + "-state-refused", st in (401, 403), (st, d))
    st, d, _ = req("GET", "/api/account", cookie=S, proto=("HTTPS",))
    check("proto-uppercase-https-ok", st == 200, (st, d))
    JUNK = "JUNKMARK-zq9<x>"
    ip_cases = {"list": "203.0.113.21, 203.0.113.22", "junk-text": JUNK, "dup": None}
    for name, v in ip_cases.items():
        extra = [("X-Real-IP", "203.0.113.23"), ("X-Real-IP", "203.0.113.24")] if v is None else []
        st, d, _ = req("POST", "/api/login", {"user": USER, "password": "wrong-password-xyz-123", "keep": False},
                       ip=v, headers=extra)
        check("realip-" + name + "-no-5xx", st in (401, 429), (st, d))
        st, d, _ = req("GET", "/api/account", cookie=S, ip=v, headers=extra)
        check("realip-" + name + "-session-ok", st == 200, (st, d))
    req("POST", "/api/login", {"user": USER, "password": PW2, "keep": False}, ip=JUNK)

    # ---- 3. parallel tries from one address
    PIP = new_ip()
    par, dropped = [], []

    def wrong():
        r = req("POST", "/api/login", {"user": USER, "password": "not-the-password-0000", "keep": False},
                       ip=PIP, timeout=60)
        par.append(r[0])
        if r[0] < 0:
            dropped.append(r[1][:80])
    ts = [threading.Thread(target=wrong) for _ in range(20)]
    [t.start() for t in ts]
    [t.join(70) for t in ts]
    check("parallel-no-hang", len(par) == 20 and -2 not in par, par)
    # a connection the kernel refuses (listen backlog full) is a clean refusal, not a 5xx; any other garbage is not
    check("parallel-answers-or-clean-refusal", all(b"Error" in x or b"refused" in x.lower() for x in dropped), dropped)
    if dropped:
        print("  note: %d of 20 connections refused by the socket: %s" % (len(dropped), dropped[0]))
    check("parallel-no-5xx", not any(500 <= s < 600 for s in par), par)
    check("parallel-throttled", par.count(401) <= 6 and 429 in par, sorted(par))
    st, d, _ = req("GET", "/api/auth/state")
    check("parallel-board-alive", st == 200 and isinstance(d, dict) and d.get("mode") == "login", (st, d))
    st, d, _ = req("POST", "/api/login", {"user": USER, "password": PW2, "keep": False}, ip=PIP)
    check("parallel-addr-still-waits", st == 429 and isinstance(d, dict) and "retry" in d, (st, d))

    # ---- 5. CSRF on every account POST and the four sign-in POSTs
    posts = ["/api/account/password", "/api/account/totp/start", "/api/account/totp/confirm", "/api/account/totp/off",
             "/api/account/recovery", "/api/account/sessions/end", "/api/account/notice/dismiss",
             "/api/login", "/api/login/code", "/api/setup-account", "/api/logout"]
    for p in posts:
        body = {"password": PW2, "user": USER, "code": "000000", "keep": False}
        st, d, _ = req("POST", p, body, cookie=S, csrf=False)
        check("csrf-no-xsubs" + p, st == 403, (st, d))
        st, d, _ = req("POST", p, body, cookie=S, headers=[("Origin", "https://evil.example")])
        check("csrf-origin-evil" + p, st == 403, (st, d))
        st, d, _ = req("POST", p, body, cookie=S, headers=[("Sec-Fetch-Site", "cross-site")])
        check("csrf-cross-site" + p, st == 403, (st, d))
    check("csrf-session-survived", req("GET", "/api/account", cookie=S)[0] == 200)

    # ---- 7. secrets only in the replies that hand them out
    allowed = {"/api/account/totp/start", "/api/account/totp/confirm", "/api/account/recovery"}
    needles = [SECRET.encode()] + [c.encode() for c in RC]
    leaks = [(p, n.decode()[:4]) for p, body in replies if p not in allowed for n in needles if n in body]
    check("no-secret-in-other-replies", not leaks, leaks[:5])
    check("within-time", time.time() < DEADLINE, "slow")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()
    time.sleep(0.2)

log = "".join(LOG)
check("log-no-junk-ip-text", "JUNKMARK" not in log, [l for l in LOG if "JUNKMARK" in l][:2])
secrets_ = [code, PW1, PW2] + ([SECRET] + RC if "SECRET" in globals() else [])
check("log-no-secrets", not any(s in log for s in secrets_), "a secret is in the board's output")
check("log-captured", bool(LOG), "board printed nothing")
shutil.rmtree(BASE, ignore_errors=True)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
