#!/usr/bin/env python3
"""Subplot: the only writer to the media drives (<prefix>-place.service; media rw; loopback network for the Plex refresh; .env for
the Plex token only). ARCHITECTURE.md "place.py". Every write goes through a pinned directory-fd chain opened O_DIRECTORY|O_NOFOLLOW
from the media root, an O_EXCL temp file and os.replace by dir fds; a sidecar is replaced or deleted only when its first-line
marker AND the manifest in marks/<key>.json (path, inode, sha256) both match. Decisions are re-read before every mutation."""
import urllib.parse
import fcntl, hashlib, http.client, json, os, secrets, stat, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

REFRESH_TIMEOUT = 5
DONE_LOG_MAX = 5 * 1024 * 1024


def _dir_chain(root, rel_dir):
    """fd of root/rel_dir with every component opened O_DIRECTORY|O_NOFOLLOW relative to the previous one. Raises OSError."""
    flags = os.O_RDONLY | os.O_DIRECTORY | C.NOFOLLOW
    fd = os.open(root, flags)
    for part in [p for p in rel_dir.split("/") if p]:
        try:
            nfd = os.open(part, flags, dir_fd=fd)
        finally:
            os.close(fd)
        fd = nfd
    return fd


def key_lock(key):
    """An exclusive flock on decisions/<key>.lock, shared with the board server: nothing writes a decision or touches the
    media for a key while another holder has it. Returns the fd; the caller closes it."""
    C.ensure_dir(C.DECISIONS)
    fd = os.open(os.path.join(C.DECISIONS, key + ".lock"), os.O_RDWR | os.O_CREAT | C.NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _own_fd(dfd, base, key, lang):
    """Open the existing sidecar through the directory fd and prove it is ours: regular file, marker for this key, and the
    marks manifest (path/inode/sha256, or a pending record with the same sha256). Returns the open fd or None."""
    try:
        fd = os.open(base, C.OPEN_FLAGS, dir_fd=dfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > C.MAX_SUB_BYTES:
            raise ValueError
        data = os.read(fd, C.MAX_SUB_BYTES + 1)
        marker = C.our_marker(data)
        if not marker or marker[0] != key:
            raise ValueError
        rec = (C.marks(key).get("placed") or {}).get(lang)
        if not isinstance(rec, dict):
            raise ValueError
        digest = hashlib.sha256(data).hexdigest()
        if rec.get("sha256") != digest or (not rec.get("pending") and rec.get("inode") != st.st_ino):
            raise ValueError
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except (OSError, ValueError):
        os.close(fd)
        return None


def _root_for(path, rules):
    for r in rules.get("media_roots", []):
        rr = os.path.realpath(r)
        if C.under(path, rr):
            return rr
    return None


def own_sidecar(path, key, lang, rules=None):
    """True only when the file at path is OUR sidecar for key/lang: marker for the key AND the marks manifest matches
    (path, sha256, and inode unless the record is still pending). A foreign file wearing our marker is not ours."""
    rec = (C.marks(key).get("placed") or {}).get(lang)
    if not isinstance(rec, dict) or rec.get("path") != path:
        return False
    root = _root_for(path, rules) if rules else None
    if root is None:
        raw = C.read_bytes(path, C.MAX_SUB_BYTES)
        if raw is None:
            return False
        marker = C.our_marker(raw)
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return bool(marker and marker[0] == key and stat.S_ISREG(st.st_mode) and rec.get("sha256") == hashlib.sha256(raw).hexdigest()
                    and (rec.get("pending") or rec.get("inode") == st.st_ino))
    try:
        dfd = _dir_chain(root, os.path.relpath(os.path.dirname(path), root))
    except OSError:
        return False
    try:
        fd = _own_fd(dfd, os.path.basename(path), key, lang)
        if fd is None:
            return False
        os.close(fd)
        return True
    finally:
        os.close(dfd)


def _item_ok(key, library, rules):
    """The library row when the item is still placeable: status ok, library not excluded, decision not exclude."""
    items = library.get("items") if isinstance(library.get("items"), dict) else library     # a bare items dict is accepted too
    item = items.get(key)
    if not isinstance(item, dict) or item.get("status") != "ok" or C.excluded_library(item.get("library"), rules):
        return None
    d = C.decision(key)
    if d and d.get("decision") == "exclude":
        return None
    return item


def place_one(key, lang, library, rules, env):
    """Move queue/<key>.<lang>.srt beside its video. Returns (ok, why)."""
    if not C.ITEM_KEY.fullmatch(key) or not C.LANG.fullmatch(lang):
        return False, "bad key"
    srt_q = os.path.join(C.QUEUE, f"{key}.{lang}.srt")
    json_q = os.path.join(C.QUEUE, f"{key}.{lang}.json")
    meta = C.read_json(json_q, 64 * 1024)
    if isinstance(meta, dict) and ((meta.get("lang") not in (None, lang)) or (meta.get("key") not in (None, key))):
        _to_review(srt_q, json_q, meta, "metadata names another file")   # a json that belongs to another pair never drives a write
        return False, "metadata mismatch"
    meta = meta if isinstance(meta, dict) else {}
    raw = C.read_bytes(srt_q, C.MAX_SUB_BYTES)
    if raw is None:
        return False, "queue file unreadable"
    marker = C.our_marker(raw)
    if not marker or marker[0] != key:
        _drop_pair(srt_q, json_q)
        C.done({"step": "place", "key": key, "lang": lang, "result": "discarded", "why": "queue file without our marker"})
        return False, "queue file without our marker"
    item = _item_ok(key, library, rules)
    if item is None:
        _drop_pair(srt_q, json_q)
        C.done({"step": "place", "key": key, "lang": lang, "result": "discarded", "why": "excluded or gone"})
        return False, "item excluded or gone"
    video = C.safe_media_path(item.get("path"), rules)
    if video is None:
        _to_review(srt_q, json_q, meta, "video missing")
        return False, "video missing"
    target = C.sidecar_path(video, lang, hi=bool(meta.get("hi")))
    root = _root_for(video, rules)
    if root is None:
        _to_review(srt_q, json_q, meta, "video outside the media roots")
        return False, "no root"
    rel_dir = os.path.relpath(os.path.dirname(target), root)
    base = os.path.basename(target)
    try:
        dfd = _dir_chain(root, rel_dir)
    except OSError as exc:
        _to_review(srt_q, json_q, meta, "folder refused")
        return False, f"folder refused: {exc.strerror or exc}"
    tmp = f".{base}.{secrets.token_hex(4)}.subs-tmp"
    digest = hashlib.sha256(raw).hexdigest()
    lock = key_lock(key)
    made_tmp, own, previous = False, None, None
    try:
        if (C.decision(key) or {}).get("decision") == "exclude":          # under the lock: the board cannot slip one in
            _drop_pair(srt_q, json_q)
            return False, "excluded meanwhile"
        try:
            st = os.stat(base, dir_fd=dfd, follow_symlinks=False)
            exists = True
        except FileNotFoundError:
            exists = False
        if exists:
            own = _own_fd(dfd, base, key, lang)
            if own is None:
                _to_review(srt_q, json_q, meta, "a subtitle we did not write is already there", raw)
                return False, "foreign subtitle present"
        placed = C.marks(key).get("placed")
        placed = placed if isinstance(placed, dict) else {}
        previous = dict(placed[lang]) if isinstance(placed.get(lang), dict) and not placed[lang].get("pending") else None
        placed[lang] = {"path": target, "sha256": digest, "pending": True, "at": C.now(),
                        "source": meta.get("source") or marker[1], "confidence": meta.get("confidence", marker[2])}
        C.mark(key, placed=placed)                                         # journal first: a crash leaves a record we recognise
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | C.NOFOLLOW, 0o644, dir_fd=dfd)
        made_tmp = True
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        if own is None:
            os.link(tmp, base, src_dir_fd=dfd, dst_dir_fd=dfd)            # new file: never clobbers a newcomer
        else:
            if os.fstat(own).st_ino != os.stat(base, dir_fd=dfd, follow_symlinks=False).st_ino:
                _to_review(srt_q, json_q, meta, "the subtitle changed while we were writing", raw)
                return False, "target changed"
            os.replace(tmp, base, src_dir_fd=dfd, dst_dir_fd=dfd)
        st = os.stat(base, dir_fd=dfd, follow_symlinks=False)
        placed[lang].update({"inode": st.st_ino, "pending": False})
        C.mark(key, placed=placed)
    except OSError as exc:
        if previous is not None:                                            # the old file is still the good one: keep its record
            placed[lang] = previous
            C.mark(key, placed=placed)
        return False, f"write failed: {exc.strerror or exc}"
    finally:
        if made_tmp:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        if own is not None:
            os.close(own)
        os.close(dfd)
        os.close(lock)
    _drop_pair(srt_q, json_q)
    C.done({"step": "place", "key": key, "lang": lang, "result": "placed", "source": placed[lang]["source"],
            "confidence": placed[lang]["confidence"], **{k: item.get(k) for k in ("show", "title", "season", "episode", "library")}})
    if lang.startswith("es") and meta.get("step") == "gen":
        kept = C.read_json(C.ES_KEPT, 4096) or {}
        C.write_json(C.ES_KEPT, {"kept": int(kept.get("kept", 0)) + 1})
    refresh_plex(item.get("plex_id"), env.get("PLEX_TOKEN", ""), item)
    return True, ""


def refresh_plex(plex_id, token, item=None):
    """Ask Plex to notice the new sidecar: a partial scan of the video's folder (GET /library/sections/<section>/refresh?path=)
    when the item carries section_id + plex_path (a metadata refresh alone does not always pick a sidecar up), else
    PUT /library/metadata/<id>/refresh. Loopback only; failures are logged as a code, never raised."""
    if not isinstance(plex_id, int) or plex_id <= 0 or not isinstance(token, str) or not token:
        return
    try:
        u = urllib.parse.urlsplit(C.PLEX_URL)
        host, port = u.hostname or "127.0.0.1", u.port or 32400
        conn = http.client.HTTPConnection(host, int(port), timeout=REFRESH_TIMEOUT)
        sec = item.get("section_id") if isinstance(item, dict) else None
        pp = item.get("plex_path") if isinstance(item, dict) else None
        if isinstance(sec, int) and sec > 0 and isinstance(pp, str) and pp.startswith("/"):
            folder = pp.rsplit("/", 1)[0] or "/"
            conn.request("GET", f"/library/sections/{sec}/refresh?" + urllib.parse.urlencode({"path": folder}),
                         headers={"X-Plex-Token": token, "Accept": "application/json"})
        else:
            conn.request("PUT", f"/library/metadata/{plex_id}/refresh", headers={"X-Plex-Token": token, "Accept": "application/json"})
        resp = conn.getresponse()
        resp.read(4096)
        if resp.status >= 300:
            C.log(f"plex refresh http {resp.status}")
        conn.close()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        C.log(f"plex refresh failed: {type(exc).__name__}")


def _drop_pair(srt, js):
    for p in (srt, js):
        try:
            os.unlink(p)
        except OSError:
            pass


def _to_review(srt, js, meta, why, raw=None):
    name = os.path.basename(srt)
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta["why"] = why
    if raw is None:
        raw = C.read_bytes(srt, C.MAX_SUB_BYTES)
    if raw is None:
        C.log(f"review move: {name} unreadable; left in place")
        return
    C.write_bytes(os.path.join(C.REVIEW, name), raw)
    C.write_json(os.path.join(C.REVIEW, name[:-4] + ".json"), meta)
    _drop_pair(srt, js)
    C.done({"step": "place", "key": meta.get("key"), "lang": meta.get("lang"), "result": "review", "why": why})


def apply_decision(key, dec, library, rules, env=None):
    """Act on one decision file (ARCHITECTURE 'place.py' step 1) under the key lock, re-reading it first, then stamp acted_at.
    Returns a short result string. env carries PLEX_TOKEN so a delete can ask Plex to rescan the folder (else Plex keeps
    listing a sidecar that is gone)."""
    if not isinstance(key, str) or not C.ITEM_KEY.fullmatch(key):
        return "bad key"
    lock = key_lock(key)
    try:
        current = C.decision(key)
        if current is None:                                                 # called with a dict and no file yet: record it first
            dec = {**dec, "at": dec.get("at") or C.now(), "by": dec.get("by", "direct")}
            C.write_json(os.path.join(C.DECISIONS, key + ".json"), dec)
            current = dec
        if current.get("acted_at") or (dec.get("at") and current.get("at") != dec.get("at")):
            return "superseded"                                             # a newer decision arrived; it gets its own turn
        dec = current
        decision = dec.get("decision")
        note = str(dec.get("note") or "")[:500]
        result = "noop"
        pairs = _pairs_for(key)
        if decision == "keep":
            C.ensure_dir(C.QUEUE)
            moved = 0
            for lang in pairs.get("review", []):
                srt_r, js_r = os.path.join(C.REVIEW, f"{key}.{lang}.srt"), os.path.join(C.REVIEW, f"{key}.{lang}.json")
                srt_q, js_q = os.path.join(C.QUEUE, f"{key}.{lang}.srt"), os.path.join(C.QUEUE, f"{key}.{lang}.json")
                try:
                    os.link(srt_r, srt_q)                                  # no clobber: an existing queue pair wins
                except FileExistsError:
                    C.log(f"keep {key} {lang}: a queue pair already exists; left as is")
                    continue
                except OSError as exc:
                    C.log(f"keep {key} {lang}: {exc.strerror or exc}")
                    continue
                try:
                    os.link(js_r, js_q)
                except OSError as exc:                                     # never leave a half pair in queue/
                    try:
                        os.unlink(srt_q)
                    except OSError:
                        pass
                    C.log(f"keep {key} {lang}: {exc.strerror or exc}")
                    continue
                _drop_pair(srt_r, js_r)
                moved += 1
            result = "queued" if moved else "nothing to keep"
        elif decision == "shift":
            C.ensure_dir(C.INCOMING)
            moved = 0
            for lang in pairs.get("review", []):
                srt_r, js_r = os.path.join(C.REVIEW, f"{key}.{lang}.srt"), os.path.join(C.REVIEW, f"{key}.{lang}.json")
                meta = C.read_json(js_r, 256 * 1024)
                off = meta.get("offset_ms") if isinstance(meta, dict) else None
                raw = C.read_bytes(srt_r, C.MAX_SUB_BYTES)
                if not isinstance(off, (int, float)) or isinstance(off, bool) or raw is None:
                    C.log(f"shift {key} {lang}: no measured offset; left in review")
                    continue
                off = int(off)
                try:
                    cues = C.parse_srt(C.strip_marker(raw))
                except C.SubtitleError:
                    C.log(f"shift {key} {lang}: file does not parse; left in review")
                    continue
                shifted = [(a + off, max(a + off + 1, b + off), t) for a, b, t in cues if b + off > 0]
                shifted = [(max(0, a), b, t) for a, b, t in shifted]
                marker = C.our_marker(raw) or (key, str(meta.get("source") or "shift"), float(meta.get("confidence") or 0.7))
                if not shifted:
                    C.log(f"shift {key} {lang}: nothing left after the shift; left in review")
                    continue
                out_meta = {k: v for k, v in meta.items() if k not in ("why", "preview", "offset_ms")}
                out_meta["shifted_by"] = int(out_meta.get("shifted_by") or 0) + off
                out_meta["at"] = C.now()
                srt_i, js_i = os.path.join(C.INCOMING, f"{key}.{lang}.srt"), os.path.join(C.INCOMING, f"{key}.{lang}.json")
                if os.path.lexists(srt_i) or os.path.lexists(js_i):
                    C.log(f"shift {key} {lang}: an incoming pair already exists; left in review")
                    continue
                try:
                    C.write_json(js_i, out_meta)                           # json first: sync needs the pair, never a lone srt
                    C.write_bytes(srt_i, C.write_srt(shifted, marker))
                except (OSError, ValueError) as exc:
                    for pth in (srt_i, js_i):
                        try:
                            os.unlink(pth)
                        except OSError:
                            pass
                    C.log(f"shift {key} {lang}: {exc}")
                    continue
                _drop_pair(srt_r, js_r)
                C.done({"step": "place", "key": key, "lang": lang, "result": "shifted", "why": f"by {off / 1000:.1f} s; re-checking",
                        **{k: library.get("items", {}).get(key, {}).get(k) for k in ("show", "title", "season", "episode", "library")}})
                moved += 1
            result = "shifted; re-checking" if moved else "nothing to shift"
        elif decision == "regenerate":
            rid = C.new_request_id()
            if C.link_request(os.path.join(C.REQUESTS, rid + ".json"), {"id": rid, "kind": "generate", "key": key, "note": note, "at": C.now()}):
                for lang in pairs.get("review", []):
                    _drop_pair(os.path.join(C.REVIEW, f"{key}.{lang}.srt"), os.path.join(C.REVIEW, f"{key}.{lang}.json"))
                result = "regenerate queued"
            else:
                result = "regenerate not queued"
        elif decision == "delete":
            placed = C.marks(key).get("placed")
            placed = placed if isinstance(placed, dict) else {}
            failed = 0
            deleted_any = False
            for lang, rec in list(placed.items()):
                path = rec.get("path") if isinstance(rec, dict) else None
                root = _root_for(path, rules) if isinstance(path, str) and C.safe_media_path(path, rules) else None
                if root is None:
                    placed.pop(lang, None)                                 # nothing of ours there any more
                    continue
                try:
                    dfd = _dir_chain(root, os.path.relpath(os.path.dirname(path), root))
                except OSError:
                    failed += 1
                    continue
                try:
                    fd = _own_fd(dfd, os.path.basename(path), key, lang)
                    if fd is None:
                        placed.pop(lang, None)                             # not ours (any more): leave it, forget it
                        continue
                    try:
                        if os.fstat(fd).st_ino != os.stat(os.path.basename(path), dir_fd=dfd, follow_symlinks=False).st_ino:
                            failed += 1
                            continue
                        os.unlink(os.path.basename(path), dir_fd=dfd)
                        placed.pop(lang, None)
                        deleted_any = True
                        C.done({"step": "place", "key": key, "lang": lang, "result": "deleted"})
                    finally:
                        os.close(fd)
                except OSError as exc:
                    failed += 1
                    C.log(f"delete failed: {exc.strerror or exc}")
                finally:
                    os.close(dfd)
            C.mark(key, placed=placed or None)
            if not failed:
                for lang in pairs.get("review", []):
                    _drop_pair(os.path.join(C.REVIEW, f"{key}.{lang}.srt"), os.path.join(C.REVIEW, f"{key}.{lang}.json"))
            if deleted_any:
                item = (library or {}).get("items", {}).get(key) if isinstance(library, dict) else None
                if isinstance(item, dict):
                    refresh_plex(item.get("plex_id"), (env or {}).get("PLEX_TOKEN", ""), item)   # Plex drops the gone sidecar
            result = "deleted" if not failed else "delete failed; try again"
        elif decision == "exclude":
            for folder, langs in pairs.items():
                for lang in langs:
                    _drop_pair(os.path.join(getattr(C, folder.upper()), f"{key}.{lang}.srt"), os.path.join(getattr(C, folder.upper()), f"{key}.{lang}.json"))
            result = "excluded"
        if result == "delete failed; try again":
            C.append_log(C.ACTION_LOG, f"decision {key} {decision} -> {result}")
            return result                                                   # no acted_at: the next run tries again
        dec["acted_at"] = C.now()
        dec["result"] = result
        C.write_json(os.path.join(C.DECISIONS, key + ".json"), dec)
        C.append_log(C.ACTION_LOG, f"decision {key} {decision} -> {result}")
        return result
    finally:
        os.close(lock)


def _pairs_for(key):
    out = {}
    for folder, path in (("incoming", C.INCOMING), ("queue", C.QUEUE), ("review", C.REVIEW)):
        langs = []
        try:
            for n in os.listdir(path):
                if n.startswith(key + ".") and n.endswith(".srt"):
                    langs.append(n[len(key) + 1:-4])
        except OSError:
            pass
        out[folder] = langs
    return out


def rotate_done_log():
    try:
        if os.lstat(C.DONE_LOG).st_size <= DONE_LOG_MAX:
            return
    except OSError:
        return
    raw = C.read_bytes(C.DONE_LOG, 64 * 1024 * 1024)
    if raw is None:                                                         # unreadable or over the read cap: keep the tail
        try:
            with open(C.DONE_LOG, "rb") as fh:
                fh.seek(-DONE_LOG_MAX // 2, 2)
                raw = fh.read()
        except OSError:
            return
    lines = raw.split(b"\n")
    C.write_bytes(C.DONE_LOG, b"\n".join(lines[len(lines) // 2:]))     # write_bytes is temp + replace


PASSES = 3


def unacted_decisions():
    """Item keys with a saved decision that has no acted_at yet."""
    try:
        names = sorted(os.listdir(C.DECISIONS))
    except OSError:
        return []
    out = []
    for name in names:
        key = name[:-5] if name.endswith(".json") else ""
        dec = C.decision(key) if C.ITEM_KEY.fullmatch(key) else None
        if dec and not dec.get("acted_at"):
            out.append((key, dec))
    return out


def decisions_pass(library, rules, env=None):
    """Act on every unacted decision once. Returns how many were tried."""
    todo = unacted_decisions()
    for key, dec in todo:
        try:
            apply_decision(key, dec, library, rules, env)
        except Exception as exc:
            C.log(f"decision {key}: {type(exc).__name__}: {exc}")
    return len(todo)


def queue_pass(library, rules, env):
    """Place every queue/<key>.<lang>.srt."""
    try:
        queue = sorted(n for n in os.listdir(C.QUEUE) if n.endswith(".srt"))
    except OSError:
        queue = []
    for name in queue:
        key, _, lang = name[:-4].rpartition(".")
        if not C.ITEM_KEY.fullmatch(key) or not C.LANG.fullmatch(lang):
            continue
        try:
            ok, why = place_one(key, lang, library, rules, env)
            if not ok:
                C.log(f"place {key} {lang}: {why}")
        except Exception as exc:
            C.log(f"place {key} {lang}: {type(exc).__name__}: {exc}")


def main():
    rules = C.load_rules()
    if rules is None:
        return 0
    env = {}
    env["PLEX_TOKEN"] = os.environ.get("PLEX_TOKEN", "")        # read once here, handed to place_one, used only in a header
    library = C.read_json(C.LIBRARY, 200 * 1024 * 1024) or {"items": {}}
    for d in (C.QUEUE, C.REVIEW, C.DECISIONS):
        C.ensure_dir(d)
    for _ in range(PASSES):                                         # a press made during this run gets its own pass, not the timer
        decisions_pass(library, rules, env)
        queue_pass(library, rules, env)
        if not unacted_decisions():
            break
    rotate_done_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
