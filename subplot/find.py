#!/usr/bin/env python3
"""Subplot: ask OpenSubtitles.com for the gaps (<prefix>-find.service; reads .env; public network only; no media access).
ARCHITECTURE.md "find.py" and "Security model". The only process that holds the OpenSubtitles key, login and JWT. Rules enforced
here: HTTPS to api.opensubtitles.com and to a download host under .opensubtitles.com only, one redirect to the same rule,
response caps, secrets only in headers, every stored error a short reason code, no external program, no media file opened.
Downloaded bytes go through common.validate_subtitle before anything is written; the sync gate (sync.py) decides later."""
import calendar, http.client, json, math, os, socket, ssl, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

API_HOST = "api.opensubtitles.com"
API_BASE = "/api/v1"
DOWNLOAD_SUFFIX = ".opensubtitles.com"
USER_AGENT = os.environ.get("SUBS_USER_AGENT", "subplot v1.0")   # OpenSubtitles wants "<app> v<version>"
MIN_INTERVAL, RETRY_DEFAULT_S, RETRY_MAX_S = 0.25, 3.0, 15.0       # the API allows about 5 requests/s; 429 = slow down, 406 = quota
TIMEOUT = 20
JSON_CAP, SUB_CAP = 1024 * 1024, 2 * 1024 * 1024
TOKEN_TTL = 23 * 3600
MAX_SEARCH_FACTOR = 5                                    # searches per day = 5x the download budget (3x stopped day one at 752 of 1000 downloads)
RUN_BUDGET_S = 50 * 60
API_LANG = {"en": "en", "es-419": "es", "es": "es"}


class HttpClient:
    """Minimal HTTPS client with a hard size cap, no automatic redirects and an absolute deadline. request() returns
    (status, headers, body); every transport problem becomes RuntimeError with a fixed reason code (never the provider's text)."""

    def __init__(self, deadline=None, pause=time.sleep):
        self.ctx = ssl.create_default_context()
        self.deadline = deadline
        self.pause = pause
        self.last = 0.0

    def request(self, method, url, headers, body, cap):
        """One request, paced to MIN_INTERVAL between calls; a 429 (the provider's per-second throttle, not the daily quota) is
        retried once after Retry-After (capped) and only then raised as 'http 429'."""
        self.pause(max(0.0, self.last + MIN_INTERVAL - time.monotonic()))
        self.last = time.monotonic()
        status, hdrs, data = self._request(method, url, headers, body, cap)
        if status == 429:
            try:
                wait = min(RETRY_MAX_S, max(1.0, float(hdrs.get("retry-after", RETRY_DEFAULT_S))))
            except ValueError:
                wait = RETRY_DEFAULT_S
            self.pause(wait)
            self.last = time.monotonic()
            status, hdrs, data = self._request(method, url, headers, body, cap)
        return status, hdrs, data

    def _request(self, method, url, headers, body, cap):
        if len(url) > 2048:
            raise RuntimeError("url too long")
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or not u.hostname:
            raise RuntimeError("https only")
        left = TIMEOUT if self.deadline is None else min(TIMEOUT, self.deadline - time.monotonic())
        if left <= 1:
            raise RuntimeError("run deadline")
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=left, context=self.ctx)
        try:
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(cap + 1)
            if len(data) > cap:
                raise RuntimeError("response too big")
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        except (http.client.HTTPException, OSError, ValueError) as exc:
            raise RuntimeError(f"transport {type(exc).__name__}") from None
        finally:
            conn.close()


def host_ok(url, api=False):
    """True for https URLs on the API host (api=True) or on a *.opensubtitles.com download host."""
    if not isinstance(url, str) or any(ord(ch) < 0x21 or ord(ch) == 0x7f for ch in url):
        return False                                                      # control characters and spaces: header/URL injection
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        return False
    host = u.hostname.lower()
    if api:
        return host == API_HOST
    return host == API_HOST or host.endswith(DOWNLOAD_SUFFIX)


def _headers(env, token=None, json_body=False):
    h = {"Api-Key": env["OPENSUBTITLES_API_KEY"], "User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _json(body):
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def os_login(client, env):
    """POST /login -> token string. Cached in tokens/os.json for TOKEN_TTL. Raises RuntimeError with a reason code."""
    cached = C.read_json(os.path.join(C.TOKENS, "os.json"), 8192)
    if isinstance(cached, dict) and isinstance(cached.get("token"), str) and 16 <= len(cached["token"]) <= 4096 \
            and isinstance(cached.get("at"), (int, float)) and 0 <= time.time() - cached["at"] < TOKEN_TTL:
        return cached["token"]
    body = json.dumps({"username": env["OPENSUBTITLES_USER"], "password": env["OPENSUBTITLES_PASS"]}).encode()
    status, _, raw = client.request("POST", f"https://{API_HOST}{API_BASE}/login", _headers(env, json_body=True), body, JSON_CAP)
    if status != 200:
        raise RuntimeError(f"http {status}")                            # bare code: the caller's 401/5xx/406 rules apply to login too
    obj = _json(raw)
    token = obj.get("token") if obj else None
    if not isinstance(token, str) or not 16 <= len(token) <= 4096:
        raise RuntimeError("login: no token")
    C.ensure_dir(C.TOKENS)
    C.write_json(os.path.join(C.TOKENS, "os.json"), {"token": token, "at": time.time()})
    return token


def _digits(v):
    v = str(v or "").strip()
    return v if v.isascii() and v.isdigit() and len(v) <= 12 and int(v) > 0 else None


def _imdb_num(v):
    """'tt0096697' -> '96697' (the API's canonical form); None when it is not an imdb id."""
    v = str(v or "").strip().lower()
    v = v[2:] if v.startswith("tt") else v
    if not v.isascii() or not v.isdigit() or len(v) > 12 or int(v) == 0:
        return None
    return str(int(v))


def os_search(client, token, item, lang, env):
    """GET /subtitles for one item. Returns a list of candidate dicts {file_id, score, trusted, downloads, hash_match, hi}.
    Raises RuntimeError('http 429') style codes; a 4xx other than 429 returns []."""
    ids = item.get("ids") or {}
    # The API answers 301 to anything but its canonical URL: parameters sorted, no parameter at its default value
    # (machine_translated=exclude is the default, so it is never sent), imdb ids without "tt" and leading zeros.
    params = {"languages": API_LANG.get(lang, lang), "ai_translated": "exclude"}
    imdb, tmdb = _imdb_num(ids.get("imdb")), _digits(ids.get("tmdb"))
    # Plex gives every episode its own imdb/tmdb id and OpenSubtitles lists episodes as features of their own, so one id
    # names the exact episode (checked live 2026-11-07: Simpsons S01E01 by imdb 348034 -> 16 English files). No tvdb search.
    if imdb:
        params["imdb_id"] = imdb
    elif tmdb:
        params["tmdb_id"] = tmdb
    else:
        return []
    url = f"https://{API_HOST}{API_BASE}/subtitles?" + urllib.parse.urlencode(sorted(params.items()))   # the API answers 301 to unsorted parameters
    status, _, raw = client.request("GET", url, _headers(env, token), None, JSON_CAP)
    if status in (429, 406):
        raise RuntimeError(f"http {status}")
    if status == 401:
        raise RuntimeError("http 401")
    if status >= 500 or 300 <= status < 400:
        raise RuntimeError(f"http {status}")                          # transient: no asked_at, retried next run
    if status == 404:
        return []
    if status != 200:
        raise RuntimeError(f"http {status}")
    obj = _json(raw)
    data = (obj or {}).get("data")
    if not isinstance(data, list):
        return []
    out = []
    for row in data[:100]:
        attrs = row.get("attributes") if isinstance(row, dict) else None
        if not isinstance(attrs, dict):
            continue
        files = attrs.get("files") if isinstance(attrs.get("files"), list) else []
        fid = files[0].get("file_id") if files and isinstance(files[0], dict) else None
        if not isinstance(fid, int) or isinstance(fid, bool) or not 0 < fid < 10 ** 12:
            continue
        out.append({"file_id": fid, "score": score_of(attrs),
                    "trusted": attrs.get("from_trusted") is True, "downloads": int(_num(attrs.get("download_count"))),
                    "hash_match": attrs.get("moviehash_match") is True, "hi": attrs.get("hearing_impaired") is True,
                    "language": str(attrs.get("language") or "")[:8]})
    return out


def _num(v, lo=0, hi=1e9):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else 0.0


def score_of(attrs):
    """Our 0-100 score for one search result (ARCHITECTURE "find.py"): 40 trusted uploader, 30 hash match, up to 30 for downloads
    (10 per decade: 100 -> 20, 1000+ -> 30), up to 10 for the site rating (0-10 scale; a 0-100 value is scaled down).
    The site's own `ratings` is 0 for most files, so it never gates on its own."""
    score = 40.0 if attrs.get("from_trusted") is True else 0.0
    score += 30.0 if attrs.get("moviehash_match") is True else 0.0
    score += min(30.0, 10.0 * math.log10(_num(attrs.get("download_count")) + 1))
    rating = _num(attrs.get("ratings"), 0, 100)
    score += min(10.0, rating if rating <= 10 else rating / 10)
    return round(min(score, 100.0), 1)


def pick_best(results, rules):
    """min_score first, then trusted, then hash match, then downloads. None when nothing qualifies.
    OpenSubtitles' own quality signal here is 'points'/'ratings' normalised to 0..100 by os_search."""
    floor = rules["find"]["min_score"]
    ok = [r for r in results if r.get("score", 0) >= floor]
    if not ok:
        return None
    ok.sort(key=lambda r: (r.get("trusted", False), r.get("hash_match", False), r.get("downloads", 0), r.get("score", 0)), reverse=True)
    return ok[0]


def os_download(client, token, file_id, env):
    """POST /download -> link (host under .opensubtitles.com) -> bytes. Returns (bytes, remaining | None).
    Raises RuntimeError with a reason code ('http 429', 'link host refused', 'download http 404', ...)."""
    body = json.dumps({"file_id": int(file_id), "sub_format": "srt"}).encode()
    status, _, raw = client.request("POST", f"https://{API_HOST}{API_BASE}/download", _headers(env, token, json_body=True), body, JSON_CAP)
    if status in (429, 406):
        raise RuntimeError(f"http {status}")
    if status != 200:
        raise RuntimeError(f"download http {status}")
    obj = _json(raw) or {}
    link, remaining = obj.get("link"), obj.get("remaining")
    if not isinstance(link, str) or len(link) > 2048 or not host_ok(link):
        raise RuntimeError("link host refused")
    reset = obj.get("reset_time_utc")
    os_download.last_reset = reset if isinstance(reset, str) and len(reset) <= 40 else None
    status, headers, data = client.request("GET", link, {"User-Agent": USER_AGENT}, None, SUB_CAP)
    if status in (301, 302, 303, 307, 308):
        target = headers.get("location", "")
        if not host_ok(target):
            raise RuntimeError("redirect host refused")
        status, headers, data = client.request("GET", target, {"User-Agent": USER_AGENT}, None, SUB_CAP)
    if status != 200:
        raise RuntimeError(f"file http {status}")
    if not isinstance(data, (bytes, bytearray)) or len(data) > SUB_CAP:
        raise RuntimeError("file too big")                               # the client caps too; this holds for any client
    rem = remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else None
    return data, rem


def candidates(library, rules, marks_all, decisions_all, busy_keys, requests):
    """[(key, lang, request_id | None)] in priority order: 'find' requests first (a show request expands to its items with
    need), then items with need ordered by views desc, library priority, added_at desc. Skips excluded, asked within
    retry_days, burned_in, and key+lang in busy_keys (incoming/queue/review)."""
    items = library.get("items", {})
    retry_s = rules["find"]["retry_days"] * 86400
    prio = {name: i for i, name in enumerate(rules.get("priority", {}).get("libraries", []))}
    now_t = time.time()

    def eligible(key, item, lang):
        if key in busy_keys or f"{key}.{lang}" in busy_keys or item.get("status") != "ok" or C.excluded_library(item.get("library"), rules):
            return False
        d = decisions_all.get(key)
        if d and d.get("decision") == "exclude":
            return False
        m = marks_all.get(key) or {}
        if m.get("burned_in"):
            return False
        asked = m.get("asked_at")
        if isinstance(asked, (int, float)) and now_t - asked < retry_s:
            return False
        return lang in (item.get("need") or [])

    out, seen = [], set()
    for rid, req in requests:
        if req.get("kind") != "find":
            continue
        if isinstance(req.get("key"), str):
            keys = [req["key"]]
        elif req.get("show") is not None and str(req.get("show")).isdigit():
            keys = [k for k, it in items.items() if str(it.get("show_plex_id")) == str(req.get("show")) or (it.get("kind") == "mv" and str(it.get("plex_id")) == str(req.get("show")))]
        else:
            keys = []
        matched = False
        for k in keys:
            it = items.get(k)
            if not it:
                continue
            for lang in it.get("need") or []:
                if (k, lang) not in seen and eligible(k, it, lang):
                    seen.add((k, lang)); out.append((k, lang, rid)); matched = True
        if not matched:
            out.append((None, None, rid))                                   # nothing to do for it: still consumed (unlinked) by main
    rest = []
    for k, it in items.items():
        for lang in it.get("need") or []:
            if (k, lang) in seen or not eligible(k, it, lang):
                continue
            views = sum(v for v in (it.get("views") or {}).values() if isinstance(v, int))
            rest.append((-views, prio.get(it.get("library"), 99), -(it.get("added_at") or 0), k, lang))
    rest.sort()
    out += [(k, lang, None) for _, _, _, k, lang in rest]
    return out


def main():
    rules = C.load_rules()
    if rules is None:
        C.log("no valid rules; nothing found")
        return 0
    env = {}
    for var in ("OPENSUBTITLES_API_KEY", "OPENSUBTITLES_USER", "OPENSUBTITLES_PASS"):
        v = os.environ.get(var, "")
        if not v:
            C.log(f"{var} is not set; find skipped")
            return 0
        env[var] = v
    library = C.read_json(C.LIBRARY, 200 * 1024 * 1024) or {"items": {}}
    busy = set()
    for folder in (C.INCOMING, C.QUEUE, C.REVIEW):
        try:
            busy |= {n[:-4] for n in os.listdir(folder) if n.endswith(".srt")}
        except OSError:
            pass
    items = library.get("items", {})
    marks_all = {k: C.marks(k) for k in items}
    decisions_all = {k: C.decision(k) for k in items}
    todo = candidates(library, rules, marks_all, decisions_all, busy, C.requests_pending())
    if not todo:
        return 0
    started = time.monotonic()
    client = HttpClient(deadline=started + RUN_BUDGET_S)
    try:
        jwt = os_login(client, env)              # the session JWT; only ever placed in the Authorization header
    except RuntimeError as exc:
        code = str(exc)
        if code.startswith("http 406"):
            _exhaust()
        C.log(f"login failed: {code[:60]}")
        return 0
    limit = rules["find"]["daily_budget"]
    done_requests, relogged = set(), False
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))                 # a stop still runs finally/atexit: current.json goes
    import atexit
    atexit.register(C.clear_current)                                     # whatever the exit path, the progress row goes
    run_started = C.now()
    for i, (key, lang, rid) in enumerate(todo, 1):
        if rid and rid not in done_requests and key is None:              # a request that matched nothing: consume it
            done_requests.add(rid)
            try:
                os.unlink(os.path.join(C.REQUESTS, rid + ".json"))
            except OSError:
                pass
            C.log(f"request {rid} had nothing to do")
            continue
        if not isinstance(key, str) or not C.ITEM_KEY.fullmatch(key) or not C.LANG.fullmatch(lang or ""):
            continue
        if time.monotonic() - started > RUN_BUDGET_S:
            break
        if not (_imdb_num((items[key].get("ids") or {}).get("imdb")) or _digits((items[key].get("ids") or {}).get("tmdb"))):
            continue                                                        # no searchable id: no unit spent, no mark
        if not C.budget_take("searches", 1, limit * MAX_SEARCH_FACTOR):
            C.log("search budget spent for today")
            break
        if rid and rid not in done_requests:
            done_requests.add(rid)
            try:
                os.unlink(os.path.join(C.REQUESTS, rid + ".json"))
            except OSError:
                pass
            C.log(f"request {rid} picked up")
        item = items[key]
        meta = {k: item.get(k) for k in ("show", "title", "season", "episode", "library")}
        C.set_current(step="find", phase="searching", key=key, lang=lang, n=i, of=len(todo), started_at=run_started, **meta)
        stop = False
        try:
            try:
                results = os_search(client, jwt, item, lang, env)
            except RuntimeError as exc:
                if str(exc) == "http 401" and not relogged:                 # the JWT expired: one fresh login, one retry
                    relogged = True
                    try:
                        os.unlink(os.path.join(C.TOKENS, "os.json"))
                    except OSError:
                        pass
                    jwt = os_login(client, env)
                    results = os_search(client, jwt, item, lang, env)
                else:
                    raise
            best = pick_best(results, rules)
            if best is None:
                C.mark(key, asked_at=time.time())
                C.done({"step": "find", "key": key, "lang": lang, "result": "none", **meta})
                continue
            if not C.budget_take("downloads", 1, limit):
                C.log("download budget spent for today")
                break
            C.set_current(phase="downloading")
            raw, remaining = os_download(client, jwt, best["file_id"], env)
            if remaining == 0:
                _exhaust(getattr(os_download, "last_reset", None))
                stop = True                                                 # finish this one, then stop until the reset
        except RuntimeError as exc:
            code = str(exc)
            if code.startswith("http 406"):
                _exhaust(getattr(os_download, "last_reset", None))
                C.log("provider says: daily quota reached; stopping until the reset")
                break
            if code.startswith("http 429"):
                C.log("provider says: too fast; stopping this run (the budget is untouched)")   # the next run continues
                break
            if code == "run deadline":
                break
            transient = code.startswith("http 5") or code.startswith("transport") or code == "http 401"
            if not transient:
                C.mark(key, asked_at=time.time())
            C.done({"step": "find", "key": key, "lang": lang, "result": "failed", "why": code[:60], **meta})
            continue
        cues, why = C.validate_subtitle(raw, lang)
        if cues is None:
            C.mark(key, asked_at=time.time())
            C.done({"step": "find", "key": key, "lang": lang, "result": "rejected", "why": why, **meta})
            if stop: break
            continue
        confidence = round(min(1.0, max(0.0, best.get("score", 0) / 100.0)), 4)
        source = f"os:{best['file_id']}"
        srt_path, json_path = os.path.join(C.INCOMING, f"{key}.{lang}.srt"), os.path.join(C.INCOMING, f"{key}.{lang}.json")
        try:
            C.write_json(json_path, {"key": key, "lang": lang, "source": source, "confidence": confidence,
                         "score": best.get("score", 0), "hi": best.get("hi", False), "at": C.now(), "step": "find"})
            C.write_bytes(srt_path, C.write_srt(cues, (key, source, confidence)))
        except ValueError as exc:                                          # the writer refused a cue: this file only, never the run
            for pth in (srt_path, json_path):
                try:
                    os.unlink(pth)
                except OSError:
                    pass
            C.mark(key, asked_at=time.time())
            C.done({"step": "find", "key": key, "lang": lang, "result": "rejected", "why": f"writer: {exc}"[:60], **meta})
            if stop:
                break
            continue
        except OSError as exc:
            for pth in (srt_path, json_path):                              # never leave half a pair behind
                try:
                    os.unlink(pth)
                except OSError:
                    pass
            C.done({"step": "find", "key": key, "lang": lang, "result": "failed", "why": "write failed", **meta})
            if stop:
                break
            continue
        C.mark(key, asked_at=time.time())                                   # only after durable output
        C.done({"step": "find", "key": key, "lang": lang, "result": "found", "source": source, "confidence": confidence, **meta})
        if stop:
            break
    return 0


def _exhaust(reset_iso=None):
    """Record the provider's reset time (bounded to 24 h ahead) or now + 24 h."""
    until = time.time() + 86400
    if isinstance(reset_iso, str):
        try:
            t = calendar.timegm(time.strptime(reset_iso[:19], "%Y-%m-%dT%H:%M:%S"))
            if time.time() < t < time.time() + 86400:
                until = t
        except ValueError:
            pass
    C.budget_exhaust(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)))


if __name__ == "__main__":
    sys.exit(main())
