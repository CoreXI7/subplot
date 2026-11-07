#!/usr/bin/env python3
"""Subplot: validators and helpers shared by scan.py, find.py, sync.py, gen.py, place.py and server.py. Python 3.12 stdlib only.
ARCHITECTURE.md "common.py" and "Security model". Everything that turns outside data (Plex rows, provider files, PC results, page
requests) into a path, a decision or a stored string lives here, so it is reviewed and tested in one place. Rules: no file
is opened through a symlink or without an O_NONBLOCK regular-file check; no secret or signed URL is ever written or logged
(redact() guards every log() and every stored reason); the excluded library is refused by name here, not only in rules."""
import calendar
import datetime, fcntl, hashlib, json, math, os, re, secrets, stat, sys, time

HOME = os.environ.get("SUBS_HOME", os.path.expanduser("~"))
STATE = os.environ.get("SUBS_STATE", os.path.join(HOME, ".local/state/subplot"))
HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.environ.get("SUBS_RULES", os.path.join(HERE, "rules.json"))
ENV_FILE = os.environ.get("SUBS_ENV", os.path.join(HERE, ".env"))
PLEX_DB_NIGHTLY = os.environ.get("SUBS_PLEX_DB", os.path.join(HOME, "plex-library.db"))   # a regularly refreshed COPY of Plex's DB
PLEX_URL = os.environ.get("SUBS_PLEX_URL", "http://127.0.0.1:32400")
PC = os.environ.get("SUBS_PC", "gpu-pc")
_PC_ROOT_RE = re.compile(r"[A-Za-z]:[\\/][A-Za-z0-9_-]{1,60}")
PC_ROOT = os.environ.get("SUBS_PC_ROOT", "C:/Subplot")
if not _PC_ROOT_RE.fullmatch(PC_ROOT):
    raise SystemExit("SUBS_PC_ROOT must look like C:/Subplot")
PC_ROOT = PC_ROOT.replace("/", "\\")                                  # the worker side is Windows; config uses forward slashes
_MARKER_TAG_RE = re.compile(r"[a-z][a-z0-9-]{2,31}")
MARKER_TAG = os.environ.get("SUBS_MARKER_TAG", "subplot")              # the word on the last line of every file we write
if not _MARKER_TAG_RE.fullmatch(MARKER_TAG):
    raise SystemExit("SUBS_MARKER_TAG must be 3-32 of a-z 0-9 -, starting with a letter")
FFMPEG = os.environ.get("SUBS_FFMPEG", "/usr/bin/ffmpeg")
FFPROBE = os.environ.get("SUBS_FFPROBE", "/usr/bin/ffprobe")

LIBRARY = os.path.join(STATE, "library.json")
COVERAGE = os.path.join(STATE, "coverage.json")
PLEX_DB_COPY = os.path.join(STATE, "plex.db")
RULES_LAST_GOOD = os.path.join(STATE, "rules.last-good.json")
INCOMING = os.path.join(STATE, "incoming")
QUEUE = os.path.join(STATE, "queue")
REVIEW = os.path.join(STATE, "review")
DECISIONS = os.path.join(STATE, "decisions")
KICK = os.path.join(DECISIONS, ".kick")                  # the board touches it after a decision; <prefix>-place.path watches it
MARKS = os.path.join(STATE, "marks")
REQUESTS = os.path.join(STATE, "requests")
BUDGET = os.path.join(STATE, "budget.json")
TOKENS = os.path.join(STATE, "tokens")
PC_STATUS = os.path.join(STATE, "pc.json")
GEN_HOURS = os.path.join(STATE, "gen_hours.json")
ES_KEPT = os.path.join(STATE, "es_kept.json")
PC_DIR = os.path.join(STATE, "pc")
DONE_LOG = os.path.join(STATE, "done.log")
ACTION_LOG = os.path.join(STATE, "actions.log")
CURRENT = os.path.join(STATE, "current.json")
PC_OVERRIDE = os.path.join(DECISIONS, "pc-generate-now.json")   # the page's "Generate now" switch: {"until", "at", "by"}; not an item key        # what a step is working on right now (find.py, gen.py); absent = nothing

EXCLUDE_LIBRARIES = ("Hentai",)                     # hard-coded on purpose: rules.json can add names, never remove this one
SECRET_VARS = ("OPENSUBTITLES_API_KEY", "OPENSUBTITLES_USER", "OPENSUBTITLES_PASS", "PLEX_TOKEN")
SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-ant-[\w-]{20,}"),
    re.compile(r"\b(ghp_\w{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16})"),
    re.compile(r"discord(app)?\.com/api/webhooks/\d+/"),
    re.compile(r"""(?i)(password|passwd|secret|token|api[_-]?key)["']?\s*[:=]\s*["']?[^\s"'$<{(\[]{8,}"""),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)X-Plex-Token=[A-Za-z0-9_-]{8,}"),
]
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")

PLEX_ID = re.compile(r"\d{1,10}")
LANG = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,4})?")
ITEM_KEY = re.compile(r"(ep|mv)-\d{1,10}")
DECISION = re.compile(r"keep|shift|regenerate|delete|exclude")
REQUEST_KIND = re.compile(r"find|generate")
REQUEST_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
SRT_TIME = re.compile(r"(\d\d):(\d\d):(\d\d)[,.](\d{3})")
USERNAME = re.compile(r"[A-Za-z0-9_.@-]{1,60}")
PROCNAME = re.compile(r"[A-Za-z0-9_.-]{1,60}")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HOURS = re.compile(r"(?:(mon|tue|wed|thu|fri|sat|sun)(?:-(mon|tue|wed|thu|fri|sat|sun))? )?([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d")
OLLAMA_URL = re.compile(r"http://(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}:\d{2,5}")   # the PC's Ollama, LAN only, plain http
MACHINE_ID = re.compile(r"[0-9a-f]{32,40}")
MARKER = re.compile(r"^; " + MARKER_TAG + r" ((?:ep|mv)-\d{1,10}) ([A-Za-z0-9:._-]{1,80}) (0(?:\.\d{1,4})?|1(?:\.0{1,4})?)\s*$")   # same charset write_srt accepts
TAG_OK = re.compile(r"</?(i|b)>", re.I)
TAG_ANY = re.compile(r"<[^<>]*>")

MAX_SUB_BYTES = 2 * 1024 * 1024
MAX_CUES = 20000
MAX_CUE_MS = 60000
MAX_REQUESTS = 50
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
OPEN_FLAGS = os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)


class SubtitleError(ValueError):
    pass


# ---------------------------------------------------------------- time and ids

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def night_id(now_local=None):
    """The date whose 18:00 most recently passed: generation hours are counted per 'night' starting at 18:00 local."""
    t = now_local or datetime.datetime.now()
    if t.hour < 18:
        t = t - datetime.timedelta(days=1)
    return t.strftime("%Y-%m-%d")


def new_request_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def item_key(kind, plex_id):
    kind = {"episode": "ep", "ep": "ep", "movie": "mv", "mv": "mv"}.get(kind)
    if kind is None or not isinstance(plex_id, int) or not 0 < plex_id < 10 ** 10:
        raise ValueError("bad item")
    return f"{kind}-{plex_id}"


# ---------------------------------------------------------------- logging and redaction

def redact(text):
    """Remove the four secret values (when set in this process's environment), token-shaped strings and control characters."""
    s = "" if text is None else str(text)
    for var in SECRET_VARS:
        v = os.environ.get(var)
        if v:
            s = s.replace(v, "[secret]")
    for rx in SECRET_RES:
        s = rx.sub("[secret]", s)
    return CONTROL.sub(" ", s)


def log(msg):
    print(f"subs: {redact(msg)}"[:2000], file=sys.stderr, flush=True)


def append_log(path, line):
    ensure_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | NOFOLLOW, 0o600)
    try:
        os.write(fd, (now() + " " + redact(line).replace("\n", " ")[:1000] + "\n").encode("utf-8", "replace"))
    finally:
        os.close(fd)


def done(entry):
    """One JSON line in DONE_LOG. Only the spec's fields, every string redacted and capped; never a path."""
    keep = ("step", "key", "show", "title", "season", "episode", "library", "lang", "result", "why", "source", "confidence", "measured_ms")
    row = {"at": now()}
    for k in keep:
        v = entry.get(k)
        if isinstance(v, str):
            v = redact(v)[:200]
        elif isinstance(v, bool) or v is None:
            pass
        elif isinstance(v, (int, float)):
            v = v if v == v and abs(v) < 1e12 else None
        else:
            v = None
        row[k] = v
    ensure_dir(STATE)
    fd = os.open(DONE_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8", "replace"))
    finally:
        os.close(fd)


# ---------------------------------------------------------------- safe files

def ensure_dir(path):
    """Create path and missing parents as 0700 directories whatever the umask is."""
    missing, cur = [], os.path.abspath(path)
    while cur and not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        os.chmod(d, 0o700)


def open_regular(path, cap):
    """fd of a regular file of at most cap bytes, no symlink, non-blocking (a FIFO can neither hang nor be read); else None."""
    try:
        fd = os.open(path, OPEN_FLAGS)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            os.close(fd)
            return None
        return fd
    except OSError:
        os.close(fd)
        return None


def read_bytes(path, cap):
    fd = open_regular(path, cap)
    if fd is None:
        return None
    try:
        parts, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                return None
            parts.append(chunk)
        return b"".join(parts)
    except OSError:
        return None
    finally:
        os.close(fd)


def tail_text(path, n):
    """Last n bytes of a regular file as text, or ''."""
    fd = open_regular(path, 1 << 40)
    if fd is None:
        return ""
    try:
        size = os.fstat(fd).st_size
        if size > n:
            os.lseek(fd, size - n, os.SEEK_SET)
        return os.read(fd, n + 1).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


def read_json(path, cap):
    raw = read_bytes(path, cap)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


def write_json(path, obj, mode=0o600):
    ensure_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_bytes(path, data, mode=0o600):
    ensure_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def link_request(path, obj):
    """Create a file that must not exist yet (temp + os.link). True when this call created it."""
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        ensure_dir(os.path.dirname(path))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        os.link(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def sha256_file(path):
    fd = open_regular(path, 1 << 40)
    if fd is None:
        return None
    try:
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def under(path, root):
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def lexical_media_path(path, rules):
    """scan.py's check: the mapped host path is absolute, normalised (no '..', no '//'), free of control characters and under a
    configured media root, WITHOUT touching the filesystem (<prefix>-scan has no media access; sync/gen/place run
    safe_media_path when they open the file). Returns the path or None."""
    if not isinstance(path, str) or not path.startswith("/") or CONTROL.search(path) or "\x00" in path:
        return None
    if os.path.normpath(path) != path or "/../" in path + "/":
        return None
    roots = [os.path.normpath(r) for r in rules.get("media_roots", []) if isinstance(r, str) and r.startswith("/")]
    return path if any(under(path, r) and path != r for r in roots) else None


def safe_media_path(path, rules):
    """A host path of a video: absolute, under a media root, every component real (no symlink anywhere), a regular file,
    name without control characters. Returns the path or None."""
    if not isinstance(path, str) or not path.startswith("/") or CONTROL.search(path) or "\x00" in path:
        return None
    roots = [os.path.realpath(r) for r in rules.get("media_roots", []) if isinstance(r, str) and r.startswith("/")]
    try:
        real = os.path.realpath(path)
        if real != os.path.normpath(path):                 # a symlink somewhere in the chain changed it
            return None
        if not any(under(real, r) for r in roots):
            return None
        st = os.lstat(real)
        if not stat.S_ISREG(st.st_mode):
            return None
        # every directory between the root and the file must itself be a real directory
        root = next(r for r in roots if under(real, r))
        cur = os.path.dirname(real)
        while cur != root:
            if os.path.islink(cur):
                return None
            cur = os.path.dirname(cur)
        return real
    except (OSError, StopIteration):
        return None


def sidecar_path(video, lang, hi=False):
    if not LANG.fullmatch(lang or ""):
        raise ValueError("bad language")
    stem, _ = os.path.splitext(video)
    return f"{stem}.{lang}{'.sdh' if hi else ''}.srt"      # .sdh is Plex's hearing-impaired tag (.hi would read as Hindi)


# ---------------------------------------------------------------- subtitles

def our_marker(text):
    """(key, source, confidence) when the LAST non-empty line is our marker (where write_srt puts it: Plex ignores a sidecar
    whose first line is not a cue index, found 2026-11-07), or the first line (files written before that day); else None."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return None
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    tail = next((l for l in reversed(lines) if l.strip()), "")
    for cand in (tail, lines[0] if lines else ""):
        m = MARKER.match(cand.strip())
        if m:
            return m.group(1), m.group(2), float(m.group(3))
    return None


def strip_marker(raw):
    """The subtitle bytes without our marker line, wherever it sits (first or last line); other bytes untouched."""
    data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
    lines = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    while lines and not lines[-1].strip():
        lines.pop()
    def is_marker(b):
        return bool(MARKER.match(b.decode("utf-8", "replace").lstrip("\ufeff").strip()))
    if lines and is_marker(lines[-1]):
        lines.pop()
    if lines and is_marker(lines[0]):
        lines.pop(0)
    return (b"\n".join(lines).strip(b"\n") + b"\n")


def _ms(h, m, s, ms):
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)


def _fmt(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(data):
    """SRT bytes or text -> [(start_ms, end_ms, text)]; raises SubtitleError. Lenient on numbering, strict on times."""
    if isinstance(data, bytes):
        text = _decode(data)
    else:
        text = data
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues, block = [], []
    lines = text.split("\n") + [""]
    for i, line in enumerate(lines):
        starts_cue = (len(block) >= 2 and line.strip().isdigit() and i + 1 < len(lines) and "-->" in lines[i + 1])
        if line.strip() == "" or starts_cue:                                 # a blank line ends a cue; so does the next index+time pair
            if block:
                cues.append(_cue(block))
                block = []
            if starts_cue:
                block.append(line)
            continue
        block.append(line)
    cues = [c for c in cues if c is not None]
    if not cues:
        raise SubtitleError("no cues")
    return cues


def _cue(block):
    lines = list(block)
    if lines and lines[0].strip().isdigit():
        lines = lines[1:]
    if not lines or lines[0].startswith(";"):
        return None                                      # a comment / our marker line
    times = lines[0]
    m = re.match(rf"\s*{SRT_TIME.pattern}\s*-->\s*{SRT_TIME.pattern}", times)
    if not m:
        raise SubtitleError("bad time line")
    g = m.groups()
    start, end = _ms(*g[:4]), _ms(*g[4:])
    return start, end, "\n".join(lines[1:]).strip()


def _decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            pass
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            pass
    return raw.decode("latin-1")


def write_srt(cues, marker):
    """Our normalized SRT: numbered cues, then the marker as the last line. marker = (key, source, confidence)."""
    key, source, conf = marker
    if not ITEM_KEY.fullmatch(key) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,80}", source):
        raise ValueError("bad marker")
    conf = float(conf)
    if not math.isfinite(conf):
        raise ValueError("bad confidence")
    conf = min(1.0, max(0.0, conf))
    out = []
    for i, (a, b, t) in enumerate(cues, 1):
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (a, b)) or a < 0 or b <= a or b >= 100 * 3600 * 1000:
            raise ValueError("bad cue")                                 # never write what the parser would refuse
        t = CONTROL.sub(" ", str(t)).replace("\r", "")
        out += [str(i), f"{_fmt(a)} --> {_fmt(b)}", t, ""]
    out += [f"; {MARKER_TAG} {key} {source} {conf:.4f}", ""]              # LAST line: Plex ignores a sidecar whose first line is not a cue index
    return ("\n".join(out)).encode("utf-8")


def validate_subtitle(raw, lang):
    """SECURITY.md "How it's fenced in": returns (cues, why). cues None on refusal. Accepts SRT, or ASS/SSA converted to SRT, and
    applies every limit AFTER conversion. Text keeps only <i>/<b> tags."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) == 0:
        return None, "empty"
    if len(raw) > MAX_SUB_BYTES:
        return None, "too big"
    try:
        text = _decode(bytes(raw))
    except Exception:
        return None, "undecodable"
    if len(text.encode("utf-8")) > MAX_SUB_BYTES:
        return None, "too big"                                  # a cp1252 file can double in size once decoded
    if re.search(r"^[ \t]*\[(script info|events)\][ \t]*$", text[:4000], re.I | re.M) or re.search(r"^[ \t]*\[events\][ \t]*$", text, re.I | re.M):   # [ \t]* not \s*: \s* crossed newlines and went quadratic on newline-only input (break-it round)
        try:
            text = ass_to_srt(text)
        except SubtitleError as exc:
            return None, f"ass: {exc}"
        if len(text.encode("utf-8")) > MAX_SUB_BYTES:
            return None, "too big after conversion"
    try:
        cues = parse_srt(text)
    except SubtitleError as exc:
        return None, str(exc)
    if len(cues) > MAX_CUES:
        return None, "too many cues"
    clean, last_start = [], -1
    for start, end, body in cues:
        if start < last_start or end < start:
            return None, "cues out of order"
        if end - start > MAX_CUE_MS:
            return None, "cue longer than 60 s"
        last_start = start
        body = TAG_ANY.sub(lambda m: m.group(0) if TAG_OK.fullmatch(m.group(0)) else "", body)
        if CONTROL.search(body.replace("\t", "")):
            return None, "control characters"
        body = body.strip()
        if len(body) > 2000:
            return None, "cue text too long"
        if body:
            clean.append((start, end, body))
    if not clean:
        return None, "no text"
    return clean, ""


def ass_to_srt(text):
    """Dialogue lines of an ASS/SSA file -> SRT text (override tags stripped, \\N -> newline). Raises SubtitleError."""
    lines = text.replace("\r\n", "\n").split("\n")
    fmt = None
    out = []
    for line in lines:
        if line.lower().startswith("format:") and fmt is None and "Start" in line:
            fmt = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
        elif line.lower().startswith("dialogue:") and fmt:
            parts = line.split(":", 1)[1].split(",", len(fmt) - 1)
            if len(parts) != len(fmt):
                continue
            row = dict(zip(fmt, [p.strip() for p in parts]))
            try:
                a, b = _ass_ms(row["start"]), _ass_ms(row["end"])
            except (KeyError, ValueError):
                continue
            raw_text = row.get("text", "")
            if re.search(r"\\p[1-9]", raw_text):
                continue                                                   # a drawing, not dialogue
            body = re.sub(r"\{[^}]*\}", "", raw_text).replace("\\N", "\n").replace("\\n", "\n").strip()
            if body:
                out.append((a, b, body))
    if not out:
        raise SubtitleError("no dialogue")
    out.sort(key=lambda c: c[0])
    srt = []
    for i, (a, b, t) in enumerate(out, 1):
        srt += [str(i), f"{_fmt(a)} --> {_fmt(b)}", t, ""]
    return "\n".join(srt)


def _ass_ms(t):
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)[.,](\d{2,3})", t.strip())
    if not m:
        raise ValueError(t)
    h, mi, s, frac = m.groups()
    return (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))


# ---------------------------------------------------------------- rules

def load_rules(path=RULES):
    """Validated rules, or the last good copy, or None. A bad file is logged with a reason code."""
    raw = read_json(path, 256 * 1024)
    why = _rules_problem(raw)
    if why is None:
        try:
            write_json(RULES_LAST_GOOD, raw)
        except OSError:
            pass
        return _normalize_rules(raw)
    log(f"rules: {why}; using the last good copy")
    old = read_json(RULES_LAST_GOOD, 256 * 1024)
    return _normalize_rules(old) if _rules_problem(old) is None else None


def _rules_problem(r):
    if not isinstance(r, dict):
        return "not an object"
    pm, roots = r.get("path_map"), r.get("media_roots")
    if not isinstance(pm, dict) or not pm or not isinstance(roots, list) or not roots:
        return "path_map and media_roots are required"
    rroots = []
    for root in roots:
        if not isinstance(root, str) or not root.startswith("/") or ".." in root.split("/"):
            return "media root must be absolute"
        try:
            st = os.stat(root)
        except OSError:
            return f"media root missing: {root}"
        if not stat.S_ISDIR(st.st_mode):
            return f"media root must be a directory: {root}"          # the drive mounts are world-writable by design; the symlink-chain checks guard writes
        rroots.append(os.path.realpath(root))
    for k, v in pm.items():
        if not isinstance(k, str) or not k.startswith("/") or not isinstance(v, str) or not v.startswith("/"):
            return "path_map entries must be absolute"
        if not any(under(os.path.realpath(v), rr) for rr in rroots):
            return f"path_map target outside the media roots: {v}"
    ex = r.get("exclude_libraries", [])
    if not isinstance(ex, list) or not all(isinstance(x, str) and 0 < len(x) < 80 for x in ex):
        return "exclude_libraries must be a list of names"
    plex = r.get("plex", {})
    if not isinstance(plex, dict) or not isinstance(plex.get("machine_id"), str) or not MACHINE_ID.fullmatch(plex["machine_id"]):
        return "plex.machine_id must be 32-40 hex characters"
    lang = r.get("languages", {})
    if not isinstance(lang, dict) or not LANG.fullmatch(str(lang.get("primary", ""))) or not LANG.fullmatch(str(lang.get("secondary", ""))):
        return "languages.primary/secondary must be language codes"
    users = lang.get("secondary_for_users", [])
    if not isinstance(users, list) or not all(isinstance(u, str) and USERNAME.fullmatch(u) for u in users):
        return "languages.secondary_for_users must be usernames"
    th = r.get("thresholds", {})
    try:
        keep, review = float(th["keep"]), float(th["review"])
    except (KeyError, TypeError, ValueError):
        return "thresholds.keep/review required"
    if not (0 <= review <= keep <= 1):
        return "thresholds must satisfy 0 <= review <= keep <= 1"
    f = r.get("find", {})
    for k in ("daily_budget", "min_score", "sync_max_offset_ms", "retry_days"):
        v = f.get(k) if isinstance(f, dict) else None
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= (100000 if k == "sync_max_offset_ms" else 1000):
            return f"find.{k} must be an integer in range"
    asm = f.get("auto_shift_max_ms", 60000)
    if not isinstance(asm, int) or isinstance(asm, bool) or not 0 <= asm <= 600000:
        return "find.auto_shift_max_ms must be 0..600000"
    g = r.get("generate", {})
    if not isinstance(g, dict) or not isinstance(g.get("model"), str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", g["model"]):
        return "generate.model"
    for k, lo, hi in (("max_hours_per_night", 1, 24), ("burned_in_frames", 4, 48), ("es_review_until_kept", 0, 1000)):
        v = g.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            return f"generate.{k}"
    ratio = g.get("burned_in_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
        return "generate.burned_in_ratio"
    idle = r.get("pc_idle", {})
    if not isinstance(idle, dict) or not isinstance(idle.get("hours"), list) or not all(isinstance(h, str) and HOURS.fullmatch(h) for h in idle["hours"]):
        return "pc_idle.hours must be a list of HH:MM-HH:MM"
    if not isinstance(idle.get("no_processes", []), list) or not all(isinstance(p, str) and PROCNAME.fullmatch(p) for p in idle.get("no_processes", [])):
        return "pc_idle.no_processes"
    gu = idle.get("gpu_util_below", 20)
    if not isinstance(gu, int) or isinstance(gu, bool) or not 0 <= gu <= 100:
        return "pc_idle.gpu_util_below"
    vf = idle.get("vram_free_mb", 6000)
    if not isinstance(vf, int) or isinstance(vf, bool) or not 0 <= vf <= 100000:
        return "pc_idle.vram_free_mb"
    if not isinstance(idle.get("yield_to", []), list) or not all(isinstance(p, str) and PROCNAME.fullmatch(p) for p in idle.get("yield_to", [])):
        return "pc_idle.yield_to"
    if not isinstance(idle.get("not_games", []), list) or not all(isinstance(p, str) and PROCNAME.fullmatch(p) for p in idle.get("not_games", [])):
        return "pc_idle.not_games"
    um = idle.get("unload_idle_model")
    if um is not None:
        if not isinstance(um, dict) or not isinstance(um.get("ollama"), str) or not OLLAMA_URL.fullmatch(um["ollama"]) \
                or not _lan_url_ok(um["ollama"]):
            return "pc_idle.unload_idle_model.ollama must be http://<LAN IPv4>:<port>"
        am = um.get("after_min", 10)
        if not isinstance(am, int) or isinstance(am, bool) or not 0 <= am <= 720:
            return "pc_idle.unload_idle_model.after_min"
    pr = r.get("priority", {})
    if not isinstance(pr, dict) or not isinstance(pr.get("watched_days", 30), int) or not 1 <= pr.get("watched_days", 30) <= 365:
        return "priority.watched_days"
    if not isinstance(pr.get("libraries", []), list) or not all(isinstance(x, str) for x in pr.get("libraries", [])):
        return "priority.libraries"
    return None


def _normalize_rules(r):
    r = json.loads(json.dumps(r))
    r["exclude_libraries"] = sorted(set(r.get("exclude_libraries", [])) | set(EXCLUDE_LIBRARIES))
    r.setdefault("pc_idle", {}).setdefault("no_processes", [])
    r["pc_idle"].setdefault("gpu_util_below", 20)
    r.setdefault("priority", {}).setdefault("watched_days", 30)
    r["priority"].setdefault("libraries", [])
    return r


def _lan_url_ok(url):
    """http://<private IPv4>:<1..65535> with real octets (the regex alone lets 10.0.0.999:99999 through)."""
    try:
        import ipaddress, urllib.parse
        u = urllib.parse.urlsplit(url)
        return u.scheme == "http" and ipaddress.IPv4Address(u.hostname).is_private and 1 <= int(u.port or 0) <= 65535
    except (ValueError, TypeError):
        return False


def excluded_library(name, rules=None):
    names = set(EXCLUDE_LIBRARIES) | set((rules or {}).get("exclude_libraries", []))
    return str(name) in names


# ---------------------------------------------------------------- marks, decisions, requests, budget

def _locked_json_update(path, cap, fn):
    ensure_dir(os.path.dirname(path))
    lock = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cur = read_json(path, cap)
        new = fn(cur if isinstance(cur, dict) else {})
        write_json(path, new)
        return new
    finally:
        os.close(lock)


def mark(key, **fields):
    """Merge fields into marks/<key>.json under a lock. gen_failed=+1 style increments use fields={'gen_failed_inc': 1}."""
    if not ITEM_KEY.fullmatch(key):
        raise ValueError("bad key")
    def upd(d):
        for k, v in fields.items():
            if k == "gen_failed_inc":
                d["gen_failed"] = int(d.get("gen_failed", 0)) + int(v)
            elif v is None:
                d.pop(k, None)
            else:
                d[k] = v
        d["at"] = now()
        return d
    return _locked_json_update(os.path.join(MARKS, key + ".json"), 64 * 1024, upd)


def marks(key):
    if not ITEM_KEY.fullmatch(key):
        return {}
    d = read_json(os.path.join(MARKS, key + ".json"), 64 * 1024)
    return d if isinstance(d, dict) else {}


def decision(key):
    """The owner's decision dict for a key, or None."""
    if not ITEM_KEY.fullmatch(key):
        return None
    d = read_json(os.path.join(DECISIONS, key + ".json"), 8192)
    return d if isinstance(d, dict) and isinstance(d.get("decision"), str) and DECISION.fullmatch(d["decision"]) else None


def requests_pending():
    """[(id, request dict)] sorted oldest first; malformed files are ignored."""
    out = []
    try:
        names = sorted(os.listdir(REQUESTS))
    except OSError:
        return out
    for name in names:
        rid = name[:-5] if name.endswith(".json") else None
        if not rid or not REQUEST_ID.fullmatch(rid):
            continue
        d = read_json(os.path.join(REQUESTS, name), 4096)
        if isinstance(d, dict) and REQUEST_KIND.fullmatch(str(d.get("kind", ""))):
            out.append((rid, d))
        if len(out) >= MAX_REQUESTS:
            break
    return out


def budget_take(kind, n, limit):
    """Take n downloads/searches from today's budget (UTC day). False when exhausted, when exhausted_until lies in the
    future, or when the limit would be passed. kind is 'downloads' or 'searches'."""
    if kind not in ("downloads", "searches") or n < 0:
        raise ValueError("bad budget call")
    result = {"ok": False}
    def upd(d):
        today = time.strftime("%Y-%m-%d", time.gmtime())             # under the lock: a midnight between call and lock is seen
        if d.get("day") != today:
            d = {"day": today, "downloads": 0, "searches": 0, "exhausted_until": d.get("exhausted_until")}
        until = d.get("exhausted_until")
        if isinstance(until, str) and not _valid_until(until):
            until = None                                      # a malformed or far-future value can't wedge the budget
        if isinstance(until, str) and until > now():
            result["ok"] = False
            return d
        d["exhausted_until"] = None
        if d.get(kind, 0) + n > limit:
            result["ok"] = False
            return d
        d[kind] = d.get(kind, 0) + n
        result["ok"] = True
        return d
    _locked_json_update(BUDGET, 8192, upd)
    return result["ok"]


def _valid_until(s):
    """Our own ISO shape (same as now()) and at most 48 h ahead."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", s):
        return False
    try:
        t = calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return False
    return t <= time.time() + 48 * 3600


def budget_exhaust(until_iso):
    def upd(d):
        d["exhausted_until"] = until_iso
        return d
    _locked_json_update(BUDGET, 8192, upd)


# ---------------------------------------------------------------- the PC

def pc_override(now_t=None):
    """True while the owner's "Generate now" switch is on: decisions/pc-generate-now.json holds an `until` in the future
    (ISO Z, at most 24 h ahead). Only the idle HOURS are waived; games, GPU users and VRAM still block."""
    cur = read_json(PC_OVERRIDE, 4096)
    until = cur.get("until") if isinstance(cur, dict) else None
    if not isinstance(until, str) or not _valid_until(until):
        return False
    t = calendar.timegm(time.strptime(until, "%Y-%m-%dT%H:%M:%SZ"))
    now_v = now_t if now_t is not None else time.time()
    return now_v < t <= now_v + 24 * 3600


def pc_idle(rules, probe, now_local=None, ignore=(), override=False):
    """(idle, why) from a probe {"gpu_util", "gpu_mem_used_mb", "gpu_mem_total_mb", "busy_procs", "processes"} and the rules'
    hours, blocked processes, GPU utilisation, other GPU users and free VRAM. `ignore` names GPU users to overlook (our worker);
    `override` (the owner's Generate now switch) waives the hours and nothing else."""
    t = now_local or datetime.datetime.now()
    idle_cfg = rules.get("pc_idle", {})
    if not isinstance(probe, dict):
        return False, "no probe"
    hhmm = t.strftime("%H:%M")
    inside = False
    for span in idle_cfg.get("hours", []):
        m = HOURS.fullmatch(span) if isinstance(span, str) else None
        if not m:
            continue
        a, b = span.rsplit(" ", 1)[-1].split("-")
        if a <= b:
            if a <= hhmm < b and span_covers_day(m.group(1), m.group(2), t.weekday()):
                inside = True
        else:                                                           # crosses midnight: the span belongs to the day it starts on
            if hhmm >= a and span_covers_day(m.group(1), m.group(2), t.weekday()):
                inside = True
            if hhmm < b and span_covers_day(m.group(1), m.group(2), (t.weekday() - 1) % 7):
                inside = True
    if not inside and not override:
        return False, "outside the idle hours"
    procs = probe.get("processes")
    if not isinstance(procs, list):
        return False, "no process list"
    blocked = sorted({str(p).lower() for p in procs if isinstance(p, str)} & {p.lower() for p in idle_cfg.get("no_processes", [])})
    if blocked:
        return False, f"{blocked[0]} is running"
    games = probe.get("game_procs")
    if not isinstance(games, list):
        return False, "no game list"                                    # the probe reports every process running from a game library
    not_games = {x.lower() for x in idle_cfg.get("not_games", [])}
    games = sorted({str(g) for g in games if isinstance(g, str) and str(g).lower() not in not_games})
    if games:
        return False, f"{games[0]} is running (game)"
    gpu = probe.get("gpu_util")
    if not isinstance(gpu, (int, float)) or isinstance(gpu, bool) or not 0 <= gpu <= 100:
        return False, "no GPU reading"
    if gpu >= idle_cfg.get("gpu_util_below", 20):
        return False, f"GPU busy ({int(gpu)}%)"
    if not isinstance(probe.get("busy_procs"), list):
        return False, "no GPU process list"                             # a reading we lack blocks, it never passes
    busy = gpu_busy_others(probe, ignore)
    if busy:
        return False, f"{busy[0]} is using the GPU"
    used, total = probe.get("gpu_mem_used_mb"), probe.get("gpu_mem_total_mb")
    need = idle_cfg.get("vram_free_mb", 6000)
    if (not isinstance(used, (int, float)) or not isinstance(total, (int, float)) or isinstance(used, bool) or isinstance(total, bool)
            or not math.isfinite(used) or not math.isfinite(total) or total <= 0 or not 0 <= used <= total):
        return False, "no VRAM reading"
    if total - used < need:
        return False, f"only {int(total - used)} MB of VRAM free"      # a loaded-but-idle model is fine while this fits
    return True, "idle"


def span_covers_day(first, last, weekday):
    """True when an hours entry's optional day tag ("mon", "mon-fri", "sat-sun", "fri-mon" wraps) includes weekday (0=Mon)."""
    if not first:
        return True
    a = DAYS.index(first)
    b = DAYS.index(last) if last else a
    return (a <= weekday <= b) if a <= b else (weekday >= a or weekday <= b)


def set_current(**fields):
    """Merge fields into current.json (the page's progress row) and stamp `at`. Never raises: progress is cosmetic."""
    try:
        cur = read_json(CURRENT, 8192)
        cur = cur if isinstance(cur, dict) else {}
        if fields.get("key") is not None and fields.get("key") != cur.get("key"):
            cur = {}                                                    # another item: nothing of the old row survives
        cur.update({k: v for k, v in fields.items() if v is not None})
        cur["at"] = now()
        write_json(CURRENT, cur)
    except (OSError, ValueError, TypeError):
        pass


def clear_current():
    try:
        os.unlink(CURRENT)
    except OSError:
        pass


def gpu_busy_others(probe, ignore=()):
    """Names of processes the probe saw working the GPU (pmon sm >= 10 %), minus `ignore` (our own worker while it runs)."""
    names = probe.get("busy_procs") if isinstance(probe, dict) else None
    if not isinstance(names, list):
        return []
    skip = {str(x).lower() for x in ignore}
    return sorted({str(n) for n in names if isinstance(n, str) and n.lower() not in skip})
