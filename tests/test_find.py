import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc")]   # the code lives in subplot/ and pc/
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="subs-find-")
os.environ["SUBS_HOME"] = _TMP
os.environ["SUBS_STATE"] = _TMP
os.environ.setdefault("OPENSUBTITLES_API_KEY", "fixture-api-key-9f3a17")
os.environ.setdefault("OPENSUBTITLES_USER", "fixture-user-b27c40")
os.environ.setdefault("OPENSUBTITLES_PASS", "fixture-pass-d51e88")
os.environ.setdefault("PLEX_TOKEN", "fixture-plex-token-6ab2c9")

import common
import find

API_KEY = os.environ["OPENSUBTITLES_API_KEY"]
USER = os.environ["OPENSUBTITLES_USER"]
PASS = os.environ["OPENSUBTITLES_PASS"]
TOKEN = "fixture-token-3ac7e1"   # privacy-ok: fake value for the redaction tests
STATE = os.environ["SUBS_STATE"]
ENV = {"OPENSUBTITLES_API_KEY": API_KEY, "OPENSUBTITLES_USER": USER,
       "OPENSUBTITLES_PASS": PASS, "PLEX_TOKEN": os.environ["PLEX_TOKEN"]}
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n"
ITEM = {"kind": "ep", "ids": {"imdb": "tt0000123", "tvdb": "123"}, "season": 1, "episode": 2}


def _b(v):
    if v is None:
        return b""
    if isinstance(v, (bytes, str)):
        return v if isinstance(v, bytes) else v.encode()
    return json.dumps(v).encode()


def _fid(picked):
    return picked.get("file_id") if isinstance(picked, dict) else picked


def _attr(fid, score, trusted, hash_match, hi, downloads):
    return {"attributes": {
        "language": "en", "download_count": downloads, "downloads": downloads,
        "ratings": score, "score": score, "from_trusted": trusted,
        "trusted": trusted, "moviehash_match": hash_match, "hash_match": hash_match,
        "hearing_impaired": hi, "file_id": fid, "files": [{"file_id": fid}]}}


def _row(fid, score, trusted):
    return {"file_id": fid, "score": score, "trusted": trusted, "downloads": 1,
            "hash_match": False, "hi": False, "language": "en"}


def _lib_item(show_plex_id):
    return {"status": "ok", "need": ["en"], "views": {}, "library": "Shows",
            "added_at": 1, "show_plex_id": show_plex_id, "kind": "ep"}


class FakeClient:
    def __init__(self):
        self.routes = []
        self.calls = []

    def add(self, method, needle, status=200, body=None, headers=None):
        self.routes.append({"method": method.upper(), "needle": needle,
                            "status": status, "body": _b(body),
                            "headers": headers or {}})

    def request(self, method, url, headers, body, cap):
        self.calls.append({"method": method.upper(), "url": url,
                           "headers": dict(headers or {}), "body": body, "cap": cap})
        for route in self.routes:
            if route["method"] == method.upper() and route["needle"] in url:
                return route["status"], dict(route["headers"]), route["body"]
        raise AssertionError("unexpected request: %s %s" % (method, url))


def os_client(token=TOKEN, link="https://dl.opensubtitles.com/sub/abc.srt"):
    client = FakeClient()
    client.add("POST", "/api/v1/login", 200, {"token": token})
    client.add("GET", "/api/v1/subtitles", 200, {"data": [
        _attr(111, 8.5, True, False, False, 120),
        _attr(222, 3.0, False, True, True, 5),
    ]})
    client.add("POST", "/api/v1/download", 200, {"link": link, "remaining": 42})
    client.add("GET", "/sub/abc.srt", 200, SRT)
    return client


class FindTests(unittest.TestCase):
    def test_search_url_is_canonical(self):
        """OpenSubtitles redirects (301) any /subtitles URL that is not canonical: parameters sorted, no default-valued
        parameter (machine_translated), imdb ids without tt and leading zeros; an episode searches by its own imdb/tmdb id."""
        import urllib.parse
        calls = []
        class Client:
            def request(self, method, url, headers, body, cap):
                calls.append(url); return 200, {}, b'{"data": []}'
        env = {"OPENSUBTITLES_API_KEY": "k"}
        find.os_search(Client(), "tok", {"kind": "ep", "ids": {"imdb": "tt0096697", "tvdb": "71663", "tmdb": "456"}, "season": 5, "episode": 1}, "en", env)
        find.os_search(Client(), "tok", {"kind": "mv", "ids": {"imdb": "tt2820466"}}, "en", env)
        find.os_search(Client(), "tok", {"kind": "ep", "ids": {"tmdb": "52156"}, "season": 1, "episode": 16}, "en", env)
        self.assertEqual(find.os_search(Client(), "tok", {"kind": "ep", "ids": {"tvdb": "71663"}, "season": 1, "episode": 1}, "en", env), [])
        self.assertEqual(len(calls), 3)
        for url in calls:
            q = urllib.parse.urlsplit(url).query
            keys = [kv.split("=")[0] for kv in q.split("&")]
            self.assertEqual(keys, sorted(keys), q)
            self.assertNotIn("machine_translated", q)
            self.assertNotIn("tvdb", q)
        self.assertIn("imdb_id=96697", calls[0]); self.assertNotIn("season_number", calls[0]); self.assertNotIn("parent_", calls[0])
        self.assertIn("imdb_id=2820466", calls[1]); self.assertNotIn("tt", urllib.parse.urlsplit(calls[1]).query)
        self.assertIn("tmdb_id=52156", calls[2]); self.assertNotIn("parent_", calls[2])

    def test_host_ok(self):
        self.assertTrue(find.host_ok("https://api.opensubtitles.com/api/v1/subtitles"))
        self.assertTrue(find.host_ok("https://api.opensubtitles.com/api/v1/login"))
        self.assertTrue(find.host_ok("https://dl.opensubtitles.com/sub/1.srt"))
        self.assertFalse(find.host_ok("https://dl.opensubtitles.com/sub/1.srt", api=True))
        self.assertFalse(find.host_ok("http://api.opensubtitles.com/api/v1/login"))
        self.assertFalse(find.host_ok("https://api.opensubtitles.com.evil.test/x"))
        self.assertFalse(find.host_ok("https://evil.test/api.opensubtitles.com"))
        self.assertFalse(find.host_ok("https://api.opensubtitles.com@evil.test/x"))

    def test_find_token_only_in_header(self):
        client = os_client()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            token = find.os_login(client, ENV)
            results = find.os_search(client, token, ITEM, "en", ENV)
            data, remaining = find.os_download(client, token, 111, ENV)
        self.assertEqual(token, TOKEN)
        self.assertEqual((data, remaining), (SRT, 42))
        by_id = {row["file_id"]: row for row in results}
        self.assertEqual(set(by_id), {111, 222})
        best, weak = by_id[111], by_id[222]
        self.assertEqual((best["score"], best["downloads"], best["language"]), (find.score_of(_attr(111, 8.5, True, False, False, 120)["attributes"]), 120, "en"))
        self.assertEqual((best["score"], weak["score"]), (69.3, 40.8))     # 40+20.8+8.5 and 30+7.8+3.0
        self.assertEqual((best["trusted"], best["hash_match"], best["hi"]), (True, False, False))
        self.assertEqual((weak["trusted"], weak["hash_match"], weak["hi"]), (False, True, True))

        login = [c for c in client.calls if "/api/v1/login" in c["url"]]
        self.assertEqual(len(login), 1)
        self.assertEqual(login[0]["method"], "POST")
        headers = {k.lower(): v for k, v in login[0]["headers"].items()}
        self.assertEqual(headers.get("api-key"), API_KEY)
        self.assertTrue(headers.get("user-agent"))
        body = json.loads(_b(login[0]["body"]))
        self.assertEqual((body.get("username"), body.get("password")), (USER, PASS))
        for call in client.calls:
            blob = _b(call["body"]) + call["url"].encode()
            self.assertNotIn(API_KEY.encode(), blob)
            self.assertNotIn(token.encode(), blob)
        for call in client.calls[1:]:
            joined = " ".join(str(v) for v in call["headers"].values())
            if "/api/v1/" in call["url"]:
                self.assertIn(token, joined)
            else:  # the file download goes to the CDN host: no token, no API key there
                self.assertNotIn(token, joined)
                self.assertNotIn(API_KEY, joined)

        self.assertTrue(os.path.exists(os.path.join(STATE, "tokens", "os.json")))
        for root, _dirs, files in os.walk(STATE):
            for name in files:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, STATE).replace(os.sep, "/")
                if rel == "tokens/os.json":
                    continue
                with open(path, "rb") as fh:
                    blob = fh.read()
                self.assertNotIn(API_KEY.encode(), blob, rel)
                self.assertNotIn(token.encode(), blob, rel)
        self.assertNotIn(API_KEY, stderr.getvalue())
        self.assertNotIn(token, stderr.getvalue())

        again = FakeClient()
        self.assertEqual(find.os_login(again, ENV), token)
        self.assertEqual([c for c in again.calls if "/login" in c["url"]], [])

    def test_download_link_host_refused(self):
        client = FakeClient()
        client.add("POST", "/api/v1/download", 200,
                   {"link": "https://evil.test/sub/x.srt", "remaining": 3})
        with self.assertRaisesRegex(RuntimeError, "link host refused"):
            find.os_download(client, TOKEN, 111, ENV)

    def test_download_redirect_host_refused(self):
        client = FakeClient()
        client.add("POST", "/api/v1/download", 200,
                   {"link": "https://dl.opensubtitles.com/sub/abc.srt", "remaining": 7})
        client.add("GET", "/sub/abc.srt", 302, b"",
                   {"Location": "https://evil.test/sub/abc.srt"})
        with self.assertRaisesRegex(RuntimeError, "redirect host refused"):
            find.os_download(client, TOKEN, 111, ENV)

    def test_search_429_raises(self):
        for status in (429, 406):
            client = FakeClient()
            client.add("GET", "/api/v1/subtitles", status, b"{}")
            with self.assertRaisesRegex(RuntimeError, "http (429|406)"):
                find.os_search(client, TOKEN, ITEM, "en", ENV)

    def test_http_client_paces_and_retries_one_429(self):
        """429 is the per-second throttle: the client waits MIN_INTERVAL between calls, retries one 429 after Retry-After
        (capped), and only a second 429 comes back as the status. A 406 (daily quota) is returned as is."""
        pauses = []
        c = find.HttpClient(pause=pauses.append)
        answers = iter([(429, {"retry-after": "2"}, b""), (200, {}, b"ok"), (429, {}, b""), (429, {}, b""), (406, {}, b"")])
        c._request = lambda *a: next(answers)
        self.assertEqual(c.request("GET", "https://api.opensubtitles.com/api/v1/x", {}, None, 100)[0], 200)
        self.assertEqual(pauses[-1], 2.0)
        self.assertEqual(c.request("GET", "https://api.opensubtitles.com/api/v1/x", {}, None, 100)[0], 429)
        self.assertEqual(pauses[-1], find.RETRY_DEFAULT_S)
        self.assertEqual(c.request("GET", "https://api.opensubtitles.com/api/v1/x", {}, None, 100)[0], 406)
        self.assertTrue(all(p >= 0 for p in pauses))
        answers = iter([(429, {"retry-after": "999"}, b""), (200, {}, b"")])
        self.assertEqual(c.request("GET", "https://api.opensubtitles.com/api/v1/x", {}, None, 100)[0], 200)
        self.assertEqual(pauses[-1], find.RETRY_MAX_S)

    def test_pick_best_min_score_and_trusted(self):
        rules = {"find": {"min_score": 70}}
        self.assertFalse(find.pick_best([_row(1, 10, True), _row(2, 69, True)], rules))
        self.assertEqual(_fid(find.pick_best([_row(3, 95, False), _row(4, 72, True)], rules)), 4)
        self.assertEqual(_fid(find.pick_best([_row(5, 80, False), _row(6, 90, False)], rules)), 6)

    def test_candidates_requests_first_and_skips(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = now - datetime.timedelta(days=1)
        old = now - datetime.timedelta(days=60)
        library = {"items": {
            "k-requested": _lib_item(100), "k-plain": _lib_item(7),
            "k-busy": _lib_item(8), "k-excluded": _lib_item(9),
            "k-recent": _lib_item(10), "k-old": _lib_item(11),
            "k-burned": _lib_item(12),
        }}
        rules = {"find": {"retry_days": 30}, "priority": {"libraries": []}}
        marks_all = {
            "k-recent": {"asked_at": recent.timestamp()},
            "k-old": {"asked_at": old.timestamp()},
            "k-burned": {"burned_in": True},
        }
        decisions_all = {"k-excluded": {"decision": "exclude"}}
        requests = [("req-fixture-1", {"kind": "find", "show": 100})]
        result = find.candidates(library, rules, marks_all, decisions_all,
                                 {"k-busy"}, requests)
        keys = [row[0] for row in result]
        self.assertEqual(keys[0], "k-requested")
        self.assertEqual(result[0][1:], ("en", "req-fixture-1"))
        self.assertEqual(set(keys), {"k-requested", "k-plain", "k-old"})
        self.assertIsNone([row for row in result if row[0] == "k-plain"][0][2])


if __name__ == "__main__":
    unittest.main()
