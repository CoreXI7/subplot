#!/usr/bin/env python3
"""Subplot: the page and API (the board; <prefix>-board.service; no network, no secrets; state read-only except
decisions/ and requests/; media inaccessible). ARCHITECTURE.md "The board". It listens only on a unix socket (0660, group
SUBS_SOCK_GROUP: the reverse proxy, which signs the owner in). No payload ever carries a host path or a row of the excluded
library; a decision is accepted only for a key with a review file (exclude: any library row); pending requests are capped."""
import calendar, fcntl, grp, http.server, json, math, os, re, socketserver, stat, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SOCK = os.environ.get("SUBS_SOCK", "/run/subplot/board.sock")
SOCK_GROUP = os.environ.get("SUBS_SOCK_GROUP", "")                    # the reverse proxy's group; empty = the service's own
HOST = os.environ.get("SUBS_HOST", "")                                 # required: serve() refuses to start without it
NAV_FILE = os.environ.get("SUBS_NAV_FILE", "")
OVERLAY = os.environ.get("SUBS_OVERLAY", "")
STATIC = os.path.join(C.HERE, "static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/subs.js": ("subs.js", "text/javascript; charset=utf-8"),
         "/subs-views.js": ("subs-views.js", "text/javascript; charset=utf-8"), "/subs.css": ("subs.css", "text/css; charset=utf-8"),
         "/base.css": ("base.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
NAV_LABEL = re.compile(r"[A-Za-z0-9 .&'_-]{1,24}")
NAV_PATH = re.compile(r"/(?!.*//)(?![/\\])[A-Za-z0-9/_.#?=&-]{0,200}")
NAV_HTTPS = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*(?::\d{1,5})?(?:/[A-Za-z0-9/_.#?=&-]{0,200})?")
_nav = []                                                              # validated at start (load_nav)
_overlay_fd = None                                                     # the overlay folder, opened once at start
LIMIT, RECENT, MAX_RESULTS, CACHE_S = 8192, 50, 50, 5
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_cache = {"at": 0.0, "key": None, "library": None, "rules": None}
_marks_cache = {"mtime": None, "data": {}}
_review_cache = {"mtime": None, "rows": None}


def all_marks():
    """{key: marks dict} for every marks/<key>.json, re-read only when the folder's mtime changes (55k items poll every 30 s)."""
    try:
        mtime = os.lstat(C.MARKS).st_mtime_ns
    except OSError:
        return {}
    if _marks_cache["mtime"] == mtime:
        return _marks_cache["data"]
    data = {}
    try:
        names = os.listdir(C.MARKS)
    except OSError:
        names = []
    for n in names:
        key = n[:-5] if n.endswith(".json") else None
        if key and C.ITEM_KEY.fullmatch(key):
            d = C.read_json(os.path.join(C.MARKS, n), 64 * 1024)
            if isinstance(d, dict):
                data[key] = d
    _marks_cache.update({"mtime": mtime, "data": data})
    return data
PUBLIC_ITEM_FIELDS = ("key", "plex_id", "kind", "library", "show_plex_id", "show", "title", "season", "episode", "year", "need", "status")


def _library():
    """library.json and rules, re-read when their mtimes change (checked at most every CACHE_S seconds)."""
    if time.monotonic() - _cache["at"] < CACHE_S and _cache["library"] is not None:
        return _cache["library"], _cache["rules"]
    try:
        key = (os.lstat(C.LIBRARY).st_mtime_ns, os.lstat(C.RULES).st_mtime_ns)
    except OSError:
        key = ("missing", time.time())                                   # never cache a failed stat: retry next time
    if key != _cache["key"] or _cache["library"] is None:
        _cache["library"] = C.read_json(C.LIBRARY, 200 * 1024 * 1024) or {"items": {}}
        rules = C.load_rules()
        _cache["rules"] = rules if rules is not None else (_cache["rules"] or {})   # never fail open on a bad rules file
        _cache["key"] = key
    _cache["at"] = time.monotonic()
    return _cache["library"], _cache["rules"]


def plex_url(rules, plex_id):
    mid = (rules.get("plex") or {}).get("machine_id", "")
    return f"https://app.plex.tv/desktop/#!/server/{mid}/details?key=/library/metadata/{int(plex_id)}" if mid and isinstance(plex_id, int) else None


def _public(item):
    return {k: item.get(k) for k in PUBLIC_ITEM_FIELDS}


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pub(v):
    """A value as the page may see it: strings redacted, cut and never a host path; numbers only when finite."""
    if isinstance(v, str):
        s = C.redact(v)[:200]
        return "[path]" if "/media/" in s or s.startswith("/") else s
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    return None


def _review_rows(library, rules):
    try:
        mtime = os.lstat(C.REVIEW).st_mtime_ns
    except OSError:
        return []
    gen_key = (mtime, _cache.get("key"))
    if _review_cache["mtime"] == gen_key and _review_cache["rows"] is not None:
        return _review_cache["rows"]
    rows = []
    try:
        names = sorted(n for n in os.listdir(C.REVIEW) if n.endswith(".json"))
    except OSError:
        names = []
    items = library.get("items") if isinstance(library.get("items"), dict) else {}
    for n in names:
        key, _, lang = n[:-5].rpartition(".")
        if not C.ITEM_KEY.fullmatch(key) or not C.LANG.fullmatch(lang):
            continue
        item = items.get(key)
        if not isinstance(item, dict) or C.excluded_library(item.get("library"), rules):
            continue                                                      # an orphan review file never reaches the page
        meta = C.read_json(os.path.join(C.REVIEW, n), 256 * 1024)
        meta = meta if isinstance(meta, dict) else {}
        conf = meta.get("confidence")
        rows.append({"key": key, "lang": lang, **{k: _pub(item.get(k)) for k in ("title", "show", "season", "episode", "library")},
                     "source": _pub(meta.get("source"))[:80] if isinstance(meta.get("source"), str) else "",
                     "confidence": conf if _finite(conf) else None,
                     "why": C.redact(str(meta.get("why") or ""))[:200],
                     "offset_ms": int(meta["offset_ms"]) if _finite(meta.get("offset_ms")) else None,
                     "shifted_by": int(meta["shifted_by"]) if _finite(meta.get("shifted_by")) else None,
                     "preview": [[c[0], c[1], C.redact(c[2])[:300]] for c in (meta.get("preview") or [])[:40]
                                 if isinstance(c, list) and len(c) == 3 and isinstance(c[2], str) and all(isinstance(x, (int, float)) for x in c[:2])],
                     "plex_url": plex_url(rules, item.get("plex_id"))})
    _review_cache.update({"mtime": gen_key, "rows": rows})
    return rows


def _tonight(night, rules):
    counts = {"found": 0, "generated": 0, "placed": 0, "burned_in": 0, "rejected": 0, "failed": 0, "since": night}
    recent = []
    raw = C.tail_text(C.DONE_LOG, 1024 * 1024).encode("utf-8", "replace")
    lines = raw.split(b"\n")[1:] if len(raw) >= 1024 * 1024 else raw.split(b"\n")     # drop a cut first line
    night_start = calendar.timegm(time.strptime(night + " 18:00", "%Y-%m-%d %H:%M")) + time.timezone   # local 18:00 as UTC epoch
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            e = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(e, dict) or C.excluded_library(e.get("library"), rules):
            continue
        if len(recent) < RECENT:
            recent.append({k: (C.redact(e.get(k)) if isinstance(e.get(k), str) else e.get(k)) for k in ("at", "step", "key", "show", "title", "season", "episode", "library", "lang", "result", "why", "source", "confidence")})
        at = str(e.get("at") or "")
        try:
            t = calendar.timegm(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if t < night_start:
            if len(recent) >= RECENT:
                break
            continue
        r, step = e.get("result"), e.get("step")
        if r == "found": counts["found"] += 1
        elif step == "gen" and r in ("queued", "review"): counts["generated"] += 1
        elif r == "placed": counts["placed"] += 1
        elif r == "burned_in": counts["burned_in"] += 1
        elif r in ("rejected", "discarded"): counts["rejected"] += 1
        elif r == "failed": counts["failed"] += 1
    return counts, recent


CURRENT_TTL = 15 * 60
CURRENT_STR = ("step", "key", "show", "title", "library", "lang", "phase", "started_at")
CURRENT_INT = ("season", "episode", "n", "of", "done_ms", "of_ms")


def current_payload(rules):
    """current.json for the page's progress row: only known fields, strings redacted and cut, ints bounded, null when
    absent, older than CURRENT_TTL (a crashed step), not find/gen, or about the excluded library."""
    cur = C.read_json(C.CURRENT, 8192)
    if not isinstance(cur, dict) or cur.get("step") not in ("find", "gen"):
        return None
    try:
        t = calendar.timegm(time.strptime(str(cur.get("at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    if not 0 <= time.time() - t <= CURRENT_TTL:
        return None
    out = {"at": str(cur.get("at"))[:20]}
    for k in CURRENT_STR:
        v = cur.get(k)
        if isinstance(v, str):
            out[k] = C.redact(v)[:120]
    for k in CURRENT_INT:
        v = cur.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 10 ** 10 and float(v).is_integer():
            out[k] = int(v)
    if C.excluded_library(out.get("library"), rules):
        return None
    return out


def _request_visible(r, items, rules):
    """A pending request is shown only while its item (or some item of its show) exists and is not excluded."""
    key = r.get("key")
    if isinstance(key, str):
        it = items.get(key)
        return isinstance(it, dict) and not C.excluded_library(it.get("library"), rules)
    show = str(r.get("show"))
    return any(str(it.get("show_plex_id")) == show and not C.excluded_library(it.get("library"), rules) for it in items.values() if isinstance(it, dict))


def state_payload():
    library, rules = _library()
    cov = C.read_json(C.COVERAGE, 4 * 1024 * 1024)
    cov = cov if isinstance(cov, dict) else {}
    libs = {}
    for name, c in (cov.get("libraries") or {}).items():
        if C.excluded_library(name, rules) or not isinstance(c, dict) or not c.get("items"):   # music/podcast sections have no video items
            continue
        libs[name] = {k: c.get(k, 0) for k in ("items", "en", "es", "none", "image_only", "generatable")}
        libs[name].update({"burned_in": 0, "ours": 0, "review": 0})
    items = library.get("items") if isinstance(library.get("items"), dict) else {}
    for key, m in all_marks().items():
        item = items.get(key)
        lib = item.get("library") if isinstance(item, dict) else None
        if lib not in libs:
            continue
        if m.get("burned_in"):
            libs[lib]["burned_in"] += 1
        if m.get("placed"):
            libs[lib]["ours"] += 1
    review = _review_rows(library, rules)
    for r in review:
        if r.get("library") in libs:
            libs[r["library"]]["review"] += 1
    night = C.night_id()
    tonight, recent = _tonight(night, rules)
    try:
        queue = sum(1 for n in os.listdir(C.QUEUE) if n.endswith(".srt"))
    except OSError:
        queue = 0
    budget = C.read_json(C.BUDGET, 8192)
    budget = budget if isinstance(budget, dict) else {}
    pc = C.read_json(C.PC_STATUS, 8192)
    pc = pc if isinstance(pc, dict) else {}
    return {"at": C.now(), "coverage": libs, "stale": bool(cov.get("stale")), "scan_at": cov.get("at"), "tonight": tonight,
            "recent": recent, "review": review, "queue": queue,
            "budget": {"downloads": budget.get("downloads", 0), "limit": (rules.get("find") or {}).get("daily_budget"), "exhausted_until": budget.get("exhausted_until")},
            "pc": {**{k: pc.get(k) for k in ("at", "idle", "why", "idle_since", "last_run", "gpu_util")}, "generate_now_until": pc_override_until()},
            "idle_hours": (rules.get("pc_idle") or {}).get("hours", []),
            "current": current_payload(rules),
            "requests": [{"id": rid, "kind": r.get("kind"), "key": r.get("key"), "show": _pub(r.get("show")), "at": _pub(r.get("at"))}
                         for rid, r in C.requests_pending() if _request_visible(r, items, rules)]}


def _status(item, lang, busy, m, d):
    if d and d.get("decision") == "exclude":
        return "excluded"
    if lang not in (item.get("need") or []):
        return "have"
    if m.get("burned_in"):
        return "burned_in"
    k = f"{item['key']}.{lang}"
    if k in busy.get("review", set()):
        return "review"
    if k in busy.get("queue", set()) or k in busy.get("incoming", set()):
        return "queued"
    return "missing"


def _busy():
    out = {}
    for name, path in (("incoming", C.INCOMING), ("queue", C.QUEUE), ("review", C.REVIEW)):
        try:
            out[name] = {n[:-4] for n in os.listdir(path) if n.endswith(".srt")}
        except OSError:
            out[name] = set()
    return out


def missing_payload(q):
    library, rules = _library()
    q = (q or "").strip().lower()[:60]
    groups, marks_all = {}, all_marks()
    for key, item in (library.get("items") or {}).items():
        if C.excluded_library(item.get("library"), rules):
            continue
        gid = item.get("show_plex_id") if item.get("kind") == "ep" else item.get("plex_id")
        name = item.get("show") if item.get("kind") == "ep" else item.get("title")
        if not isinstance(name, str) or (q and q not in name.lower()):
            continue
        g = groups.setdefault(gid, {"id": gid, "name": name, "kind": item.get("kind"), "library": item.get("library"), "items": 0, "missing_en": 0, "missing_es": 0, "burned_in": 0})
        g["items"] += 1
        need = item.get("need") or []
        m = marks_all.get(key) or {}
        if m.get("burned_in"):
            g["burned_in"] += 1
        elif "en" in need:
            g["missing_en"] += 1
        if any(l.startswith("es") for l in need):
            g["missing_es"] += 1
    rows = sorted(groups.values(), key=lambda g: (-g["missing_en"], g["name"]))[:MAX_RESULTS]
    return {"results": rows, "pending": [r.get("show") or r.get("key") for _, r in C.requests_pending()]}


def show_payload(sid):
    library, rules = _library()
    busy = _busy()
    eps = []
    for key, item in (library.get("items") or {}).items():
        if C.excluded_library(item.get("library"), rules):
            continue
        if str(item.get("show_plex_id")) != str(sid) and not (item.get("kind") == "mv" and str(item.get("plex_id")) == str(sid)):
            continue
        m, d = all_marks().get(key) or {}, C.decision(key)
        eps.append({**_public(item), "en": _status(item, "en", busy, m, d), "es": _status(item, "es-419", busy, m, d),
                    "plex_url": plex_url(rules, item.get("plex_id"))})
    eps.sort(key=lambda e: (e.get("season") or 0, e.get("episode") or 0, e.get("title") or ""))
    return {"id": sid, "episodes": eps}


def handle_request(body):
    library, rules = _library()
    if not isinstance(body, dict):
        return 400, {"why": "body must be an object"}
    kind = body.get("kind")
    if not isinstance(kind, str) or not C.REQUEST_KIND.fullmatch(kind):
        return 400, {"why": "kind must be find or generate"}
    key, show = body.get("key"), body.get("show")
    items = library.get("items") or {}
    if isinstance(key, str):
        if not C.ITEM_KEY.fullmatch(key) or key not in items or C.excluded_library(items[key].get("library"), rules):
            return 404, {"why": "unknown item"}
        target = {"key": key}
    elif isinstance(show, int) and not isinstance(show, bool) and C.PLEX_ID.fullmatch(str(show)):
        if not any((it.get("show_plex_id") == show or (it.get("kind") == "mv" and it.get("plex_id") == show)) and not C.excluded_library(it.get("library"), rules) for it in items.values()):
            return 404, {"why": "unknown show"}
        target = {"show": show}
    else:
        return 400, {"why": "key or show required"}
    pending = C.requests_pending()
    if len(pending) >= C.MAX_REQUESTS:
        return 409, {"why": "too many requests waiting; let the services catch up"}
    for _, r in pending:
        if r.get("key") == target.get("key") and target.get("key") is not None or r.get("show") == target.get("show") and target.get("show") is not None:
            return 409, {"why": "already queued"}
    note = body.get("note")
    note = C.redact(note)[:500] if isinstance(note, str) else ""
    rid = C.new_request_id()
    if not C.link_request(os.path.join(C.REQUESTS, rid + ".json"), {"id": rid, "kind": kind, **target, "note": note, "at": C.now()}):
        return 409, {"why": "could not queue; try again"}
    C.log(f"action: request {rid} {kind} {target}")        # state is read-only here: the journal is the board's action log
    return 202, {"id": rid}


def kick():
    """Touch decisions/.kick so <prefix>-place.path starts <prefix>-place.service now instead of at its next 5-min tick.
    Best effort: a failure only means the timer acts on the decision instead."""
    try:
        fd = os.open(C.KICK, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | C.NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            os.write(fd, C.now().encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def handle_decision(body):
    library, rules = _library()
    if not isinstance(body, dict):
        return 400, {"why": "body must be an object"}
    key, decision = body.get("key"), body.get("decision")
    if not isinstance(key, str) or not C.ITEM_KEY.fullmatch(key) or not isinstance(decision, str) or not C.DECISION.fullmatch(decision):
        return 400, {"why": "bad key or decision"}
    item = (library.get("items") or {}).get(key)
    if not item or C.excluded_library(item.get("library"), rules):
        return 404, {"why": "unknown item"}
    if decision != "exclude":
        try:
            names = os.listdir(C.REVIEW)
        except OSError:
            names = []
        has_review = any(n.startswith(key + ".") and n.endswith(".srt") for n in names)
        if not has_review:
            return 409, {"why": "nothing to review for this item"}
        if decision == "shift":
            offs = []
            for n in names:
                if not (n.startswith(key + ".") and n.endswith(".json")):
                    continue
                lang = n[len(key) + 1:-5]
                if not C.LANG.fullmatch(lang) or not os.path.exists(os.path.join(C.REVIEW, f"{key}.{lang}.srt")):
                    continue
                m = C.read_json(os.path.join(C.REVIEW, n), 256 * 1024)
                if isinstance(m, dict) and _finite(m.get("offset_ms")) and abs(m["offset_ms"]) <= 10 ** 9:
                    offs.append(m["offset_ms"])
            if not offs:
                return 409, {"why": "no measured timing offset for this item"}
    note = body.get("note")
    note = C.redact(note)[:500] if isinstance(note, str) else ""
    lock = os.open(os.path.join(C.DECISIONS, key + ".lock"), os.O_RDWR | os.O_CREAT | C.NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)                                   # shared with place.py: never mid-mutation
        C.write_json(os.path.join(C.DECISIONS, key + ".json"), {"decision": decision, "note": note, "at": C.now(), "by": "page"})
    finally:
        os.close(lock)
    C.log(f"action: decision {key} {decision}")
    kick()
    return 202, {"ok": True}


PC_HOURS_MAX = 12


def handle_pc(body):
    """The owner's "Generate now" switch: {"generate_now": bool, "hours": 1..12 (default 6)} -> decisions/pc-generate-now.json
    with `until` = now + hours (on) or removed (off). Only the idle hours are waived by it (common.pc_idle)."""
    if not isinstance(body, dict) or not isinstance(body.get("generate_now"), bool):
        return 400, {"why": "generate_now must be true or false"}
    hours = body.get("hours", 6)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= PC_HOURS_MAX:
        return 400, {"why": f"hours must be 1..{PC_HOURS_MAX}"}
    if body["generate_now"]:
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + hours * 3600))
        C.write_json(C.PC_OVERRIDE, {"until": until, "at": C.now(), "by": "page"})
        C.log(f"action: generate now until {until}")
        return 202, {"ok": True, "until": until}
    try:
        os.unlink(C.PC_OVERRIDE)
    except FileNotFoundError:
        pass
    except OSError:
        return 500, {"why": "could not switch it off"}
    C.log("action: generate now off")
    return 202, {"ok": True, "until": None}


def pc_override_until():
    cur = C.read_json(C.PC_OVERRIDE, 4096)
    until = cur.get("until") if isinstance(cur, dict) else None
    return until if isinstance(until, str) and C.pc_override() else None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version, sys_version, protocol_version = "subs", "", "HTTP/1.1"

    timeout = 15                                                          # a stalled client cannot hold the single thread

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:   # a torn state file must not drop the connection silently
            try:
                self.reply(500, {"why": f"server error: {type(exc).__name__}"})
            except OSError:
                pass

    def reply(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if self.command == "HEAD":
            body = b""
        self.send_response(code)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def checked(self):
        self.close_connection = True
        if not HOST or self.headers.get_all("Host") != [HOST]:
            self.reply(421 if self.headers.get_all("Host") else 400, {"why": "wrong host"}); return None
        raw = (self.requestline.split(" ") + ["", ""])[1]                  # as sent: http.server rewrites a leading '//'
        path = self.path.split("?", 1)[0]
        if not path.startswith("/") or any(bad in p for p in (path, raw.split("?", 1)[0]) for bad in ("..", "//", "\\", "%")):
            self.reply(400, {"why": "bad path"}); return None             # never decoded, so nothing can sneak past this
        return path

    def do_GET(self):
        path = self.checked()
        if path is None:
            return
        if self.headers.get("Content-Length") not in (None, "0") or self.headers.get("Transfer-Encoding"):
            return self.reply(400, {"why": "a GET carries no body"})
        if path in FILES:
            name, ctype = FILES[path]
            data = overlay_bytes(name)
            if data is None:
                data = C.read_bytes(os.path.join(STATIC, name), 1024 * 1024)
            return self.reply(200, data, ctype) if data is not None else self.reply(404, {"why": "not found"})
        if path == "/api/config":
            return self.reply(200, {"nav": _nav})
        try:
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "", max_num_fields=5)
        except ValueError:
            return self.reply(400, {"why": "bad query"})
        if path == "/api/state":
            return self.reply(200, state_payload())
        if path == "/api/missing":
            return self.reply(200, missing_payload((query.get("q") or [""])[0]))
        if path.startswith("/api/show/"):
            sid = path[len("/api/show/"):]
            if not C.PLEX_ID.fullmatch(sid):
                return self.reply(404, {"why": "not found"})
            return self.reply(200, show_payload(int(sid)))
        self.reply(404, {"why": "not found"})

    def do_POST(self):
        path = self.checked()
        if path is None:
            return
        if path not in ("/api/request", "/api/decision", "/api/pc"):
            return self.reply(404, {"why": "not found"})
        if self.headers.get("X-Subs") != "1":
            return self.reply(403, {"why": "missing X-Subs header"})
        origin, sfs = self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")
        if origin not in (None, f"https://{HOST}") or sfs not in (None, "same-origin"):
            return self.reply(403, {"why": "cross-origin request refused"})
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length") or []) > 1:
            return self.reply(400, {"why": "unsupported framing"})
        try:
            n = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            return self.reply(411, {"why": "length required"})
        if n < 0 or n > LIMIT:
            return self.reply(413, {"why": "body too large"})
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self.reply(400, {"why": "bad JSON"})
        code, payload = {"/api/request": handle_request, "/api/decision": handle_decision, "/api/pc": handle_pc}[path](body)
        self.reply(code, payload)

    def do_HEAD(self):
        self.close_connection = True
        self.reply(405, {"why": "method not allowed"})
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = do_HEAD

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.reply(405 if code == 501 else code, {"why": "method not allowed" if code == 501 else (message or "error")})


class Server(socketserver.UnixStreamServer):
    allow_reuse_address = True
    request_queue_size = 16


def load_nav(path=None):
    """The header tabs from nav.json: at most 8 {"label", "href"}; a bad entry is dropped and logged (never its text)."""
    path = NAV_FILE if path is None else path
    out = []
    if not path:
        return out
    data = C.read_json(path, 16 * 1024)
    if not isinstance(data, list):
        if data is not None:
            C.log("nav.json: not a list; no tabs")
        return out
    for i, e in enumerate(data[:8]):
        label = e.get("label") if isinstance(e, dict) else None
        href = e.get("href") if isinstance(e, dict) else None
        if isinstance(label, str) and isinstance(href, str) and NAV_LABEL.fullmatch(label) and label.strip() \
                and (NAV_PATH.fullmatch(href) or NAV_HTTPS.fullmatch(href)):
            out.append({"label": label, "href": href})
        else:
            C.log(f"nav.json: entry {i + 1} dropped (label or href not allowed)")
    return out


def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def open_overlay(path=None):
    """The overlay folder's fd (O_DIRECTORY|O_NOFOLLOW), or None when unset. Refuses STATE, the config folders and anything
    inside them (a misconfigured overlay must never serve .env or state)."""
    path = OVERLAY if path is None else path
    if not path:
        return None
    for guarded in (C.STATE, os.path.dirname(os.path.abspath(C.RULES)), os.path.dirname(os.path.abspath(C.ENV_FILE))):
        if _inside(path, guarded) or _inside(guarded, path):
            raise SystemExit(f"SUBS_OVERLAY must be its own folder, not the state or config folder")
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | C.NOFOLLOW)


def overlay_bytes(name, fd=None):
    """The overlay's copy of a bundled page file (a FILES name only), or None: opened with openat + O_NOFOLLOW, a regular
    file under 1 MiB, read from that same descriptor (no check-then-open race)."""
    fd = _overlay_fd if fd is None else fd
    if fd is None or name not in {n for n, _ in FILES.values()}:
        return None
    try:
        f = os.open(name, os.O_RDONLY | C.NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
    except OSError:
        return None
    try:
        st = os.fstat(f)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 1024 * 1024:
            return None
        chunks, left = [], 1024 * 1024 + 1
        while left > 0:
            b = os.read(f, min(65536, left))
            if not b:
                break
            chunks.append(b); left -= len(b)
        data = b"".join(chunks)
        return data if len(data) <= 1024 * 1024 else None
    finally:
        os.close(f)


def make_server(sock=SOCK, group=None):
    for d in (C.DECISIONS, C.REQUESTS):
        C.ensure_dir(d)
    C.ensure_dir(os.path.dirname(sock))
    try:
        if stat.S_ISSOCK(os.lstat(sock).st_mode):
            os.unlink(sock)
    except FileNotFoundError:
        pass
    old = os.umask(0o117)
    try:
        srv = Server(sock, Handler)
    finally:
        os.umask(old)
    group = SOCK_GROUP if group is None else group
    if group:
        os.chown(sock, -1, grp.getgrnam(group).gr_gid)                 # the proxy's group may connect; nobody else
    os.chmod(sock, 0o660)
    return srv


def serve(sock=SOCK):
    global _nav, _overlay_fd
    if not HOST:
        raise SystemExit("SUBS_HOST is required (the host name the reverse proxy sends)")
    _nav = load_nav()
    _overlay_fd = open_overlay()
    with make_server(sock) as srv:
        print(f"subplot-board: listening on {sock}", file=sys.stderr, flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    serve()
