#!/usr/bin/env python3
"""Break-it runner: OpenSubtitles is hostile. Reuses test_find's fake client and fixture environment."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc"), _os.path.join(_ROOT, "tests")]   # the code lives in subplot/ and pc/
import io, json, os, sys, contextlib

sys.argv = [sys.argv[0]]
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import test_find as T     # noqa: E402  (sets the temp state and the fixture secrets in os.environ)
import common as C             # noqa: E402
import find                    # noqa: E402

results = []
log = io.StringIO()


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else ": " + str(detail)[:160]))


def run(name, fn, expect_error=None, want=None):
    """fn either returns (checked against want) or raises RuntimeError with a short code (checked against expect_error)."""
    try:
        with contextlib.redirect_stderr(log):
            got = fn()
    except RuntimeError as exc:
        code = str(exc)
        check(name, (expect_error is not None and code.startswith(expect_error) and len(code) < 80) or expect_error == "any", code)
        return None
    except Exception as exc:
        check(name, False, f"raised {type(exc).__name__}: {exc}")
        return None
    check(name, expect_error is None and (want is None or got == want), got)
    return got


ITEM = {"kind": "mv", "ids": {"imdb": "tt2820466"}}
SECRETS = [v for k, v in T.ENV.items()] + [T.TOKEN]

# ---- login answers
for body, expect in (({"token": "x" * 5000}, "login"), ({"token": "short"}, "login"), ({"token": 5}, "login"), ("not json", "login"), ({"token": "t" * 40}, None)):
    c = T.FakeClient(); c.add("POST", "/api/v1/login", 200, body)
    try:
        os.unlink(os.path.join(C.TOKENS, "os.json"))
    except OSError:
        pass
    run(f"login-{str(body)[:16]!r}", lambda c=c: find.os_login(c, T.ENV), expect_error=expect)
for status in (301, 302, 403, 500, 503):
    c = T.FakeClient(); c.add("POST", "/api/v1/login", status, {})
    try:
        os.unlink(os.path.join(C.TOKENS, "os.json"))
    except OSError:
        pass
    run(f"login-http-{status}", lambda c=c: find.os_login(c, T.ENV), expect_error="http ")

# ---- search answers
def search(body, status=200):
    c = T.FakeClient(); c.add("GET", "/api/v1/subtitles", status, body)
    return find.os_search(c, T.TOKEN, ITEM, "en", T.ENV)
run("search-data-dict", lambda: search({"data": {"a": 1}}), want=[])
run("search-data-none", lambda: search({"data": None}), want=[])
run("search-data-ints", lambda: search({"data": [1, 2, 3]}), want=[])
run("search-body-list", lambda: search([1, 2]), want=[])
run("search-body-garbage", lambda: search("zzz"), want=[])
def row(fid, **attrs):
    a = {"language": "en", "download_count": 10, "from_trusted": False, "ratings": 0, "moviehash_match": False, "hearing_impaired": False, "files": [{"file_id": fid}]}
    a.update(attrs)
    return {"attributes": a}
for fid in ("111", 1.5, -3, 10 ** 30, None, True):
    got = run(f"search-file-id-{fid!r}", lambda f=fid: search({"data": [row(f)]}))
    if got is not None:
        check(f"search-file-id-{fid!r}-dropped", all(isinstance(r["file_id"], int) and 0 < r["file_id"] < 10 ** 12 for r in got), got)
for dc in (-5, "x", 1e308, float("nan"), None):
    got = run(f"search-downloads-{str(dc)[:5]}", lambda d=dc: search({"data": [row(7, download_count=d)]}))
    if got:
        check(f"search-downloads-{str(dc)[:5]}-finite-score", all(0 <= r["score"] <= 100 for r in got), got)
got = run("search-ratings-nan", lambda: search({"data": [row(8, ratings=float("nan"))]}))
if got:
    check("search-ratings-nan-score-finite", all(r["score"] == r["score"] and 0 <= r["score"] <= 100 for r in got), got)
got = run("search-150-rows", lambda: search({"data": [row(i + 1) for i in range(150)]}))
check("search-150-rows-capped", got is not None and len(got) <= 100, None if got is None else len(got))
for status in (301, 302, 403, 418, 500):
    run(f"search-http-{status}-transient", lambda s=status: search({}, s), expect_error="http ")
run("search-http-404-empty", lambda: search({}, 404), want=[])
run("search-http-429", lambda: search({}, 429), expect_error="http 429")
run("search-http-406", lambda: search({}, 406), expect_error="http 406")

# ---- download answers
def download(link, data=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", status=200, extra=None, remaining=5):
    c = T.FakeClient()
    c.add("POST", "/api/v1/download", 200, {"link": link, "remaining": remaining, "reset_time_utc": "2026-11-08T00:00:00Z", **(extra or {})})
    if isinstance(link, str) and link.startswith("https://"):
        c.add("GET", link.split("//", 1)[1].split("/", 1)[0], status, data)
    c.add("GET", "https://", status, data)
    out = find.os_download(c, T.TOKEN, 111, T.ENV)
    for call in c.calls:
        host = call["url"].split("//", 1)[-1].split("/", 1)[0]
        if "Api-Key" in call["headers"] and host != find.API_HOST:
            raise AssertionError(f"api key sent to {host}")
        if "Authorization" in call["headers"] and host != find.API_HOST:
            raise AssertionError(f"token sent to {host}")
    return out
for link in ("http://dl.opensubtitles.com/x.srt", "https://evil.test/x.srt", "https://api.opensubtitles.com.evil.test/x.srt",
             "https://user:pass@dl.opensubtitles.com/x.srt", "ftp://dl.opensubtitles.com/x.srt", "file:///etc/passwd", "", None, 5, "https://"):
    run(f"download-link-{str(link)[:28]!r}", lambda l=link: download(l), expect_error="any")
run("download-2mib+1", lambda: download("https://dl.opensubtitles.com/x.srt", data=b"x" * (2 * 1024 * 1024 + 1)), expect_error="any")
run("download-remaining-neg", lambda: download("https://dl.opensubtitles.com/x.srt", remaining=-1))
run("download-remaining-str", lambda: download("https://dl.opensubtitles.com/x.srt", remaining="x"))
run("download-reset-2999", lambda: download("https://dl.opensubtitles.com/x.srt", extra={"reset_time_utc": "2999-01-01T00:00:00Z"}))
run("download-reset-garbage", lambda: download("https://dl.opensubtitles.com/x.srt", extra={"reset_time_utc": {"x": 1}}))
c = T.FakeClient(); c.add("POST", "/api/v1/download", 200, {"link": "https://dl.opensubtitles.com/a", "remaining": 5})
for i in range(6):
    c.add("GET", f"https://dl.opensubtitles.com/{'abcdef'[i]}", 302, b"", headers={"location": f"https://dl.opensubtitles.com/{'bcdefg'[i]}"})
run("download-5-hop-redirect", lambda: find.os_download(c, T.TOKEN, 111, T.ENV), expect_error="any")
c = T.FakeClient(); c.add("POST", "/api/v1/download", 200, {"link": "https://dl.opensubtitles.com/a", "remaining": 5})
c.add("GET", "https://dl.opensubtitles.com/a", 302, b"", headers={"location": "https://dl.opensubtitles.com/b\r\nX: y"})
try:
    find.os_download(c, T.TOKEN, 111, T.ENV); check("download-location-newline", False, "followed a header with CRLF")
except (RuntimeError, AssertionError) as exc:
    check("download-location-newline", "\r" not in str(exc) and "\n" not in str(exc), str(exc)[:80])

# ---- host_ok corner cases
for url, api, want in (("https://api.opensubtitles.com/api/v1/x", True, True), ("https://api.opensubtitles.com@evil.test/x", True, False),
                       ("https://evil.test/api.opensubtitles.com", True, False), ("https://dl.opensubtitles.com/x", False, True),
                       ("https://opensubtitles.com.evil.test/x", False, False), ("https://xopensubtitles.com/x", False, False),
                       ("https://api.opensubtitles.com:443/x", True, True), ("https://API.OPENSUBTITLES.COM/x", True, True), ("https://[::1]/x", False, False)):
    run(f"host_ok-{url[8:40]}", lambda u=url, a=api: find.host_ok(u, api=a), want=want)

# ---- nothing secret anywhere
blob = log.getvalue()
for root, _, files in os.walk(os.environ.get("SUBS_STATE", "")):
    for f in files:
        try:
            blob += open(os.path.join(root, f), "rb").read().decode("utf-8", "replace")
        except OSError:
            pass
leaks = [s for s in SECRETS if s and s in blob and s != T.TOKEN]
check("no-secret-in-logs-or-state", not leaks, leaks)
check("token-only-in-tokens-file", all(T.TOKEN not in open(os.path.join(r, f), "rb").read().decode("utf-8", "replace") for r, _, fs in os.walk(os.environ["SUBS_STATE"]) for f in fs if not r.endswith("tokens")))
print(f"SUMMARY pass={sum(results)} fail={len(results) - sum(results)}")
