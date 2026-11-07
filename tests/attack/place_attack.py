#!/usr/bin/env python3
"""Hostile-input runner for place.py (break-it round, 2026-11-07).

Everything is built under a private temp folder. The runner drives the real
place.own_sidecar / place.place_one / place.apply_decision and prints one
PASS/FAIL line per case, then SUMMARY. Standard library only.
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc"), _os.path.join(_ROOT, "tests")]   # the code lives in subplot/ and pc/

import hashlib
import os
import stat
import sys
import tempfile

BASE = os.environ.get("SUBS_SANDBOX") or "/tmp"
ROOT = tempfile.mkdtemp(prefix="place-attack-", dir=BASE)
MEDIA = os.path.join(ROOT, "media")
HOME = os.path.join(ROOT, "home")
STATE = os.path.join(HOME, "state")
OUTSIDE = os.path.join(ROOT, "outside")
RULES_FILE = os.path.join(HOME, "rules.json")

for _d in (MEDIA, HOME, STATE, OUTSIDE):
    os.makedirs(_d, exist_ok=True)
with open(RULES_FILE, "w", encoding="utf-8") as _f:
    _f.write("{}\n")

os.environ.update({
    "SUBS_HOME": HOME,
    "SUBS_STATE": STATE,
    "SUBS_RULES": RULES_FILE,
    "SUBS_FFMPEG": "/bin/false",
    "SUBS_FFPROBE": "/bin/false",
})

CODE = os.environ.get("SUBS_CODE", "")
if CODE:
    sys.path.insert(0, CODE)

import common as C
import place as P

PASSWORD_ENV = {"PLEX_TOKEN": ""}
CUES = [(1000, 2000, "hello"), (2500, 3200, "world")]
EXCLUDED = "SecretShelf"
KEY_BASE = 5000
RULES = None
LIB_SHAPE = 0
PASSED = 0
FAILED = 0


def s(value):
    if isinstance(value, str):
        return value
    try:
        return os.fspath(value)
    except TypeError:
        return str(value)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        names = list(filenames) + list(dirnames)
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    out[rel] = ("link", os.readlink(path))
                except OSError:
                    out[rel] = ("link", "?")
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            elif stat.S_ISREG(st.st_mode):
                try:
                    digest = sha256(path)
                except OSError:
                    digest = "?"
                out[rel] = ("file", digest, stat.S_IMODE(st.st_mode))
            else:
                out[rel] = ("special", stat.S_IFMT(st.st_mode))
    return out


def diff(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def allowed_change(rel):
    return (rel == "media" or rel.startswith("media/")
            or rel == "home" or rel.startswith("home/"))


ACCEPT_WORDS = {"placed", "keep", "kept", "deleted", "shifted", "shift",
                "removed", "ok", "done", "success", "accepted"}


def is_accept(result):
    if result is True:
        return True
    if isinstance(result, str):
        return result.strip().lower() in ACCEPT_WORDS
    if isinstance(result, dict):
        for name in ("ok", "accepted", "placed", "success"):
            if result.get(name) is True:
                return True
        inner = result.get("result")
        return isinstance(inner, str) and inner.strip().lower() in ACCEPT_WORDS
    if isinstance(result, (tuple, list)) and result:
        return is_accept(result[0])
    return False


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def record(name, fails):
    global PASSED, FAILED
    if fails:
        FAILED += 1
        print("FAIL %s: %s" % (name, "; ".join(str(f) for f in fails)))
    else:
        PASSED += 1
        print("PASS %s" % name)


def guard(name, action, quiet_media=True, foreign=(), forbid=()):
    before = snapshot(ROOT)
    result = None
    failure = None
    try:
        result = action()
    except AssertionError as exc:
        failure = str(exc) or "assertion failed"
    except BaseException as exc:
        failure = "raised %s: %s" % (type(exc).__name__, str(exc)[:160])
    after = snapshot(ROOT)
    fails = []
    if failure:
        fails.append(failure)
    else:
        changed = diff(before, after)
        stray = [rel for rel in changed if not allowed_change(rel)]
        if stray:
            fails.append("changed outside media+state: " + ", ".join(stray[:3]))
        if quiet_media:
            moved = [rel for rel in changed if rel.startswith("media/")]
            if moved:
                fails.append("media changed on refusal: " + ", ".join(moved[:3]))
        for path, digest in foreign:
            try:
                now = sha256(path)
            except OSError:
                now = None
            if now != digest:
                fails.append("foreign file altered: " + os.path.basename(path))
        for path in forbid:
            if os.path.lexists(s(path)):
                fails.append("forbidden path created: " + os.path.basename(s(path)))
        if is_accept(result):
            fails.append("accepted hostile input (returned %r)" % (result,))
        if isinstance(result, str) and len(result) > 200:
            fails.append("refusal is not a short reason code")
    record(name, fails)
    return result


def make_file(path, data=b"\x1aE\xdf\xa3fake-mkv"):
    path = s(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def write_json_dir(path, obj):
    path = s(path)
    parent = os.path.dirname(path)
    if parent:
        C.ensure_dir(parent)
    C.write_json(path, obj)


def srt_bytes(marker):
    data = C.write_srt(CUES, marker)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data


def sidecar_of(video, lang="en"):
    path = s(C.sidecar_path(video, lang))
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(video), path)
    return path


def key_for(n):
    return C.item_key("movie", KEY_BASE + n)


def build_item(key, video, library="Movies", status="ok", plex_id=4242, kind="movie"):
    return {"key": str(key), "path": s(video), "library": library,
            "status": status, "plex_id": plex_id, "kind": kind}


def shape_library(item, idx):
    key = item["key"]
    if idx == 0:
        return {"items": {key: item}}
    if idx == 1:
        return {"items": [item]}
    if idx == 2:
        return {key: item}
    return {"items": [item], "libraries": [item["library"]]}


def write_library(item):
    lib = shape_library(item, LIB_SHAPE)
    write_json_dir(C.LIBRARY, lib)
    return lib


def write_queue_pair(key, lang, marker_key=None, json_lang=None, offset=None):
    C.ensure_dir(C.QUEUE)
    base = os.path.join(s(C.QUEUE), "%s.%s" % (key, lang))
    C.write_bytes(base + ".srt",
                  srt_bytes((str(marker_key or key), "opensubtitles", 0.9)))
    meta = {"lang": json_lang or lang, "source": "opensubtitles", "confidence": 0.9}
    if offset is not None:
        meta["offset_ms"] = offset
    write_json_dir(base + ".json", meta)
    return base


def write_review_pair(key, lang, offset):
    C.ensure_dir(C.REVIEW)
    base = os.path.join(s(C.REVIEW), "%s.%s" % (key, lang))
    C.write_bytes(base + ".srt",
                  srt_bytes((str(key), "opensubtitles", 0.9)))
    meta = {"lang": lang, "source": "opensubtitles", "confidence": 0.9,
            "why": "off by 2 s"}
    if offset is not None:
        meta["offset_ms"] = offset
    write_json_dir(base + ".json", meta)
    return base


def rules_candidates(media_root):
    return [
        {"media_roots": [media_root], "roots": [media_root],
         "media": [media_root], "exclude_libraries": [EXCLUDED],
         "excluded_libraries": [EXCLUDED], "libraries": ["Movies", EXCLUDED]},
        {"media_roots": [media_root], "exclude_libraries": [EXCLUDED],
         "libraries": ["Movies", EXCLUDED]},
        {"media_roots": [media_root], "excluded_libraries": [EXCLUDED]},
        {"media_roots": [{"path": media_root}], "exclude_libraries": [EXCLUDED]},
        {"roots": [media_root], "excluded_libraries": [EXCLUDED]},
        {"media": [media_root], "exclude_libraries": [EXCLUDED]},
        {"media_root": media_root, "exclude_libraries": [EXCLUDED]},
        {"media_roots": [media_root], "exclude_libraries": [EXCLUDED],
         "hours": {"start": 18, "end": 6}, "processes": [], "gpu_max": 95},
    ]


def pick_rules():
    global RULES
    probe = make_file(os.path.join(MEDIA, "__probe__", "probe.mkv"))
    fallback = None
    for cand in rules_candidates(MEDIA):
        try:
            ok_root = bool(C.safe_media_path(probe, cand))
            ok_excl = bool(C.excluded_library(EXCLUDED, cand))
        except BaseException:
            continue
        if ok_root and ok_excl:
            RULES = cand
            return
        if ok_root and fallback is None:
            fallback = cand
    RULES = fallback or rules_candidates(MEDIA)[0]


def install_rules():
    paths = [RULES_FILE]
    try:
        paths.append(s(getattr(C, "RULES", RULES_FILE)))
    except BaseException:
        pass
    for path in paths:
        try:
            write_json_dir(path, RULES)
        except BaseException:
            pass


def pick_library_shape():
    global LIB_SHAPE
    for idx in range(4):
        LIB_SHAPE = idx
        try:
            key = C.item_key("movie", 9000 + idx)
            video = make_file(os.path.join(MEDIA, "__calib%d__" % idx, "calib.mkv"))
            item = build_item(key, video, plex_id=9000 + idx)
            lib = write_library(item)
            write_queue_pair(key, "en")
            P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV))
            target = sidecar_of(video, "en")
            if os.path.isfile(target) and not os.path.islink(target):
                return
        except BaseException:
            continue
    LIB_SHAPE = 0


def ensure_dirs():
    for attr in ("QUEUE", "REVIEW", "DECISIONS", "INCOMING", "MARKS"):
        path = getattr(C, attr, None)
        if path:
            try:
                C.ensure_dir(path)
            except BaseException:
                pass


def case_symlink_video():
    key = key_for(1)
    target = make_file(os.path.join(OUTSIDE, "real-video.mp4"))
    digest = sha256(target)
    video = os.path.join(MEDIA, "c1", "movie.mkv")
    os.makedirs(os.path.dirname(video), exist_ok=True)
    os.symlink(target, video)
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en")
    guard("symlink-video",
          lambda: P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV)),
          foreign=[(target, digest)])


def case_symlink_season():
    key = key_for(2)
    real = os.path.join(OUTSIDE, "season-real")
    inside = make_file(os.path.join(real, "ep.mkv"))
    digest = sha256(inside)
    show = os.path.join(MEDIA, "c2", "Show")
    os.makedirs(show, exist_ok=True)
    os.symlink(real, os.path.join(show, "Season 1"))
    video = os.path.join(show, "Season 1", "ep.mkv")
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en")
    guard("symlink-season-folder",
          lambda: P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV)),
          foreign=[(inside, digest)])


def case_sidecar_symlink():
    key = key_for(3)
    video = make_file(os.path.join(MEDIA, "c3", "movie.mkv"))
    target = make_file(os.path.join(OUTSIDE, "precious.srt"),
                       b"1\n00:00:01,000 --> 00:00:02,000\nkeep me\n")
    digest = sha256(target)
    side = sidecar_of(video, "en")
    C.ensure_dir(os.path.dirname(side))
    if os.path.lexists(side):
        os.unlink(side)
    os.symlink(target, side)
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en")

    def action():
        got = P.own_sidecar(video, key, "en", RULES)
        require(not is_accept(got), "own_sidecar accepted a symlinked sidecar")
        return P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV))

    guard("sidecar-symlink", action, foreign=[(target, digest)])


def case_sidecar_fifo():
    if not hasattr(os, "mkfifo"):
        record("sidecar-fifo", ["platform lacks os.mkfifo"])
        return
    key = key_for(4)
    video = make_file(os.path.join(MEDIA, "c4", "movie.mkv"))
    side = sidecar_of(video, "en")
    C.ensure_dir(os.path.dirname(side))
    if os.path.lexists(side):
        os.unlink(side)
    os.mkfifo(side, 0o644)
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en")

    def action():
        got = P.own_sidecar(video, key, "en", RULES)
        require(not is_accept(got), "own_sidecar accepted a FIFO sidecar")
        return P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV))

    guard("sidecar-fifo", action)


def case_foreign_sidecar():
    key = key_for(5)
    video = make_file(os.path.join(MEDIA, "c5", "movie.mkv"))
    side = sidecar_of(video, "en")
    C.ensure_dir(os.path.dirname(side))
    with open(side, "wb") as f:
        f.write(b"1\n00:00:01,000 --> 00:00:02,000\nnot ours\n")
    digest = sha256(side)
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en")
    guard("foreign-sidecar",
          lambda: P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV)),
          foreign=[(side, digest)])


def case_marker_no_manifest():
    key = key_for(6)
    video = make_file(os.path.join(MEDIA, "c6", "movie.mkv"))
    side = sidecar_of(video, "en")
    C.ensure_dir(os.path.dirname(side))
    C.write_bytes(side, srt_bytes((str(key), "opensubtitles", 0.9)))
    digest = sha256(side)
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en")

    def action():
        P.own_sidecar(video, key, "en", RULES)
        return P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV))

    guard("marker-without-manifest", action, foreign=[(side, digest)])


def case_marker_other_key():
    key = key_for(7)
    video = make_file(os.path.join(MEDIA, "c7", "movie.mkv"))
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en", marker_key=key_for(70))
    guard("queue-marker-other-key",
          lambda: P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV)))


def case_json_lang_mismatch():
    key = key_for(8)
    video = make_file(os.path.join(MEDIA, "c8", "movie.mkv"))
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en", json_lang="es")
    guard("queue-json-lang-mismatch",
          lambda: P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV)))


def case_dotdot_path():
    key = key_for(9)
    evil = make_file(os.path.join(OUTSIDE, "evil.mkv"))
    digest = sha256(evil)
    os.makedirs(os.path.join(MEDIA, "c9"), exist_ok=True)
    video = os.path.join(MEDIA, "c9", "..", "..", "outside", "evil.mkv")
    item = build_item(key, video)
    lib = write_library(item)
    write_queue_pair(key, "en")
    guard("item-path-dotdot",
          lambda: P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV)),
          foreign=[(evil, digest)])


def case_excluded_library():
    key = key_for(10)
    video = make_file(os.path.join(MEDIA, "c10", "movie.mkv"))
    item = build_item(key, video, library=EXCLUDED)
    lib = write_library(item)
    write_queue_pair(key, "en")
    guard("item-excluded-library",
          lambda: P.place_one(key, "en", lib, RULES, dict(PASSWORD_ENV)))


def case_delete_without_review():
    key = key_for(11)
    video = make_file(os.path.join(MEDIA, "c11", "movie.mkv"))
    item = build_item(key, video)
    lib = write_library(item)
    # "delete" is a real owner decision (the board only accepts it for keys with a review file); with nothing of ours
    # placed for this key it must change nothing on disk
    before = snapshot(MEDIA)                                       # the media drives; the decision's own stamp lives in state
    try:
        P.apply_decision(key, {"decision": "delete"}, lib, RULES)
        record("decision-delete-no-review", [] if snapshot(MEDIA) == before else ["a delete with nothing placed changed the disk"])
    except BaseException as exc:
        record("decision-delete-no-review", ["raised %s" % type(exc).__name__])


def case_shift_without_review():
    key = key_for(12)
    video = make_file(os.path.join(MEDIA, "c12", "movie.mkv"))
    item = build_item(key, video)
    lib = write_library(item)
    guard("decision-shift-no-review",
          lambda: P.apply_decision(
              key, {"decision": "shift", "offset_ms": 500}, lib, RULES))


def case_traversal_key():
    key = "../x"
    video = make_file(os.path.join(MEDIA, "c13", "movie.mkv"))
    item = build_item(key_for(13), video)
    lib = write_library(item)
    forbid = [
        os.path.join(HOME, "x.json"),
        os.path.join(STATE, "x.json"),
        os.path.join(ROOT, "x.json"),
        os.path.join(os.path.dirname(s(C.DECISIONS)), "x.json"),
    ]
    guard("decision-key-traversal",
          lambda: P.apply_decision(key, {"decision": "keep"}, lib, RULES),
          forbid=forbid)


def case_shift_bad_offsets():
    for idx, (label, offset) in enumerate((("huge", 1e12),
                                           ("negative", -1e12),
                                           ("text", "x"))):
        key = key_for(20 + idx)
        video = make_file(os.path.join(MEDIA, "c14_%d" % idx, "movie.mkv"))
        item = build_item(key, video)
        lib = write_library(item)
        write_review_pair(key, "en", offset)
        guard("shift-offset-%s" % label,
              lambda key=key, lib=lib, offset=offset: P.apply_decision(
                  key, {"decision": "shift", "offset_ms": offset}, lib, RULES))


CASES = [
    case_symlink_video,
    case_symlink_season,
    case_sidecar_symlink,
    case_sidecar_fifo,
    case_foreign_sidecar,
    case_marker_no_manifest,
    case_marker_other_key,
    case_json_lang_mismatch,
    case_dotdot_path,
    case_excluded_library,
    case_delete_without_review,
    case_shift_without_review,
    case_traversal_key,
    case_shift_bad_offsets,
]


def main():
    try:
        pick_rules()
    except BaseException:
        pass
    try:
        install_rules()
    except BaseException:
        pass
    try:
        ensure_dirs()
    except BaseException:
        pass
    try:
        pick_library_shape()
    except BaseException:
        pass
    for case in CASES:
        try:
            case()
        except BaseException as exc:
            record(case.__name__, ["setup raised %s: %s"
                                   % (type(exc).__name__, str(exc)[:160])])
    print("SUMMARY pass=%d fail=%d" % (PASSED, FAILED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
