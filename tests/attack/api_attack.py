#!/usr/bin/env python3
"""Break-it runner: hostile HTTP against the real board over its unix socket. Reuses test_server's fixture."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc"), _os.path.join(_ROOT, "tests")]   # the code lives in subplot/ and pc/
import json, os, sys, socket

sys.argv = [sys.argv[0]]
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import test_server as T                       # noqa: E402  (module import builds the state + rules fixture)
import common as C                                 # noqa: E402

T.SubsServerTest.setUpClass()
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else ": " + str(detail)[:160]))


def send(method, path, body=None, host=T.HOST, headers=None):
    try:
        return T._send(method, path, body=body, host=host, headers=headers)
    except Exception as exc:                        # a dropped connection is a refusal too
        return -1, repr(exc).encode()


def raw_request(data, timeout=5):
    """Bytes straight onto the socket (for broken framing)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(T.SOCK)
        s.sendall(data)
        out = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
                if b"\r\n\r\n" in out and len(out) > 12:
                    break
        except socket.timeout:
            pass
        return out
    except OSError as exc:
        return repr(exc).encode()
    finally:
        s.close()


def status_of(raw):
    try:
        return int(raw.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return -1


POST_H = {"X-Subs": "1", "Origin": T.ORIGIN, "Content-Type": "application/json"}
state_before = {os.path.join(r, f) for r, _, fs in os.walk(T.STATE) for f in fs}
outside_before = {os.path.join(r, f) for r, _, fs in os.walk(T.BASE) for f in fs} - state_before

# ---- host / header hygiene
check("wrong-host-421", send("GET", "/api/state", host="evil.example")[0] == 421)
check("missing-host-400", send("GET", "/api/state", host=None)[0] == 400)
check("host-with-port-421", send("GET", "/api/state", host=T.HOST + ":8443")[0] in (421, 400))
check("host-with-space-4xx", send("GET", "/api/state", host=T.HOST + " x")[0] in (421, 400))
check("post-no-xsubs-403", send("POST", "/api/pc", body=b"{}", headers={"Origin": T.ORIGIN, "Content-Type": "application/json"})[0] == 403)
check("post-xsubs-2-403", send("POST", "/api/pc", body=b"{}", headers={**POST_H, "X-Subs": "2"})[0] == 403)
check("post-origin-evil-403", send("POST", "/api/pc", body=b"{}", headers={**POST_H, "Origin": "https://evil.example"})[0] == 403)
check("post-origin-null-403", send("POST", "/api/pc", body=b"{}", headers={**POST_H, "Origin": "null"})[0] == 403)
check("post-cross-site-403", send("POST", "/api/pc", body=b"{}", headers={**POST_H, "Sec-Fetch-Site": "cross-site"})[0] == 403)
# ---- bodies and framing
check("body-8193-413", send("POST", "/api/pc", body=b"{" + b" " * 8192 + b"}", headers=POST_H)[0] == 413)
check("body-1e6-413", send("POST", "/api/pc", body=b"{" + b" " * 1_000_000 + b"}", headers=POST_H)[0] in (413, -1))
check("get-with-body-400", send("GET", "/api/state", body=b"{}")[0] == 400)
st = status_of(raw_request(f"POST /api/pc HTTP/1.1\r\nHost: {T.HOST}\r\nX-Subs: 1\r\nOrigin: {T.ORIGIN}\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{{}}\r\n0\r\n\r\n".encode()))
check("chunked-post-400", st == 400, st)
st = status_of(raw_request(f"POST /api/pc HTTP/1.1\r\nHost: {T.HOST}\r\nX-Subs: 1\r\nOrigin: {T.ORIGIN}\r\nContent-Length: -5\r\n\r\n".encode()))
check("negative-length-4xx", st in (400, 411, 413), st)
st = status_of(raw_request(f"POST /api/pc HTTP/1.1\r\nHost: {T.HOST}\r\nX-Subs: 1\r\nOrigin: {T.ORIGIN}\r\nContent-Length: abc\r\n\r\n".encode()))
check("non-numeric-length-411", st in (400, 411), st)
st = status_of(raw_request(f"POST /api/pc HTTP/1.1\r\nHost: {T.HOST}\r\nX-Subs: 1\r\nOrigin: {T.ORIGIN}\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{{}}".encode()))
check("duplicate-length-400", st == 400, st)
st = status_of(raw_request(f"POST /api/pc HTTP/1.1\r\nHost: {T.HOST}\r\nX-Subs: 1\r\nOrigin: {T.ORIGIN}\r\nContent-Length: 100\r\n\r\n{{\"generate_now\": tr".encode(), timeout=3))
check("short-body-no-crash", st in (-1, 400, 408), st)
check("still-alive-after-short-body", send("GET", "/api/state")[0] == 200)
# ---- methods and paths
for m in ("PUT", "DELETE", "OPTIONS", "TRACE", "PATCH"):
    check(f"method-{m}-405", send(m, "/api/state")[0] in (405, 501))
check("lowercase-get-4xx", status_of(raw_request(f"get /api/state HTTP/1.1\r\nHost: {T.HOST}\r\n\r\n".encode())) in (400, 405, 501, -1))
check("show-traversal-404", send("GET", "/api/show/../../x")[0] in (400, 404))
check("show-encoded-dotdot-404", send("GET", "/api/show/%2e%2e")[0] in (400, 404))
check("show-huge-id-404", send("GET", "/api/show/1e9")[0] in (400, 404))
check("static-traversal-404", send("GET", "/subs.js/../server.py")[0] in (400, 404))
check("path-10kb-4xx", send("GET", "/api/show/" + "1" * 10240)[0] in (400, 404, 414, -1))
check("query-100-fields-4xx", send("GET", "/api/missing?" + "&".join(f"q{i}=x" for i in range(100)))[0] in (400, 200))
# ---- decision bodies
for key in ("../x", "ep-1;rm", "ep-99999999999", "EP-1", "ep-1\n", "mv-", "ep-1" * 30):
    code = send("POST", "/api/decision", body=json.dumps({"key": key, "decision": "keep"}).encode(), headers=POST_H)[0]
    check(f"decision-key-{key[:8]!r}-4xx", code in (400, 404, 409), code)
for dec in ("SHIFT", "keep ", "kéep", "delete​", "regenerate; rm -rf", "exclude\n"):
    code = send("POST", "/api/decision", body=json.dumps({"key": "ep-1", "decision": dec}).encode(), headers=POST_H)[0]
    check(f"decision-word-{dec[:8]!r}-400", code == 400, code)
check("shift-without-offset-409", send("POST", "/api/decision", body=json.dumps({"key": "ep-1", "decision": "shift"}).encode(), headers=POST_H)[0] == 409)
# ---- /api/pc
for body in ({"generate_now": True, "hours": 0}, {"generate_now": True, "hours": 13}, {"generate_now": True, "hours": 1e9},
             {"generate_now": True, "hours": "6"}, {"generate_now": True, "hours": True}, {"generate_now": "yes"}, {"generate_now": 1}, {"generate_now": None}, [], "x", 5):
    code = send("POST", "/api/pc", body=json.dumps(body).encode(), headers=POST_H)[0]
    check(f"pc-{str(body)[:22]}-400", code == 400, code)
# ---- hostile JSON
deep = "[" * 10000 + "]" * 10000
check("json-10000-deep-400", send("POST", "/api/pc", body=deep.encode()[:8000], headers=POST_H)[0] == 400)
check("json-invalid-utf8-400", send("POST", "/api/pc", body=b'{"generate_now": "\xff\xfe"}', headers=POST_H)[0] == 400)
check("json-duplicate-keys-400", send("POST", "/api/pc", body=b'{"generate_now": true, "generate_now": "x"}', headers=POST_H)[0] == 400)
# ---- volume
codes = [send("GET", "/api/state")[0] for _ in range(300)]
check("300-gets-all-200", all(c == 200 for c in codes), set(codes))
# ---- leaks and files
for path in ("/api/state", "/api/missing?q=show", "/api/show/100"):
    code, data = send("GET", path)
    text = data.decode("utf-8", "replace")
    check(f"no-path-leak-{path.split('?')[0]}", "/media/" not in text and "Hentai" not in text and "NaN" not in text and "Infinity" not in text)
state_after = {os.path.join(r, f) for r, _, fs in os.walk(T.STATE) for f in fs}
outside_after = {os.path.join(r, f) for r, _, fs in os.walk(T.BASE) for f in fs} - state_after
check("no-files-outside-state", outside_after == outside_before, outside_after ^ outside_before)
new_state = sorted(os.path.relpath(p, T.STATE) for p in state_after - state_before)
check("state-files-expected-only", all(p.startswith(("decisions/", "requests/")) or p == "rules.last-good.json" for p in new_state), new_state[:5])
check("decision-names-are-keys", all(os.path.basename(p).split(".")[0].startswith(("ep-", "mv-", "pc-generate")) for p in new_state if p.startswith("decisions/")), new_state[:5])
print(f"SUMMARY pass={sum(results)} fail={len(results) - sum(results)}")
