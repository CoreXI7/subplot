#!/usr/bin/env python3
"""Subplot: generate one subtitle on the GPU PC when it is idle (<prefix>-gen.service, every 10 min; network to the PC's SSH only;
media read-only; no secrets). ARCHITECTURE.md "gen.py". The PC receives audio.opus, 12 frames and a job.json of numbers and language
codes, nothing else; its output is validated here before it enters incoming/ for sync.py's gate."""
import calendar, json, math, os, pathlib, re, shutil, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

PROBE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pc", "probe.ps1")   # the repo's pc/, beside subplot/
FFMPEG_TIMEOUT, PC_TIMEOUT, SSH_TIMEOUT = 300, 40 * 60, 60
AUDIO_CAP, FRAME_CAP, SRT_CAP, RESULT_CAP = 60 * 1024 * 1024, 200 * 1024, 2 * 1024 * 1024, 4096
FRAMES = 12
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")           # an Ollama tag, nothing else goes into the POST
OLLAMA_TIMEOUT, OLLAMA_CAP = 20, 65536


def real_run(args, timeout):
    import subprocess
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, b"", "timed out"
    except OSError as exc:
        return 127, b"", str(exc)



run = real_run                                           # tests replace this runner

def ssh_args(cmd):
    return ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", C.PC, cmd]


def probe_pc(run):
    """Run pc/probe.ps1 on the PC (its text is sent as the command, like the Agents board does). Returns the parsed dict or
    None. The reply is untrusted: 64 KiB cap, JSON only, fields checked by C.pc_idle."""
    try:
        script = pathlib.Path(PROBE).read_text(encoding="utf-8")
    except OSError:
        return None
    rc, out, _ = run(ssh_args(script), SSH_TIMEOUT)
    if rc != 0 or not out or len(out) > 65536:
        return None
    try:
        obj = json.loads(out.decode("utf-8", "replace").lstrip("\ufeff"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def real_http(method, url, body=None):
    """One plain-http call to the PC's Ollama (LAN only, checked by the rules validator). Returns the reply bytes (capped)
    or None. No secrets travel: Ollama has no auth."""
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            return resp.read(OLLAMA_CAP + 1)
    except (OSError, ValueError):
        return None


def ollama_loaded(url, http):
    """Names of the models the PC's Ollama holds in memory (GET /api/ps), validated; [] when none or unreadable."""
    raw = http("GET", url + "/api/ps")
    if not raw or len(raw) > OLLAMA_CAP:
        return []
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return []
    models = obj.get("models") if isinstance(obj, dict) else None
    names = []
    for m in models if isinstance(models, list) else []:
        n = m.get("name") if isinstance(m, dict) else None
        if isinstance(n, str) and MODEL_NAME.fullmatch(n) and "://" not in n and ".." not in n:
            names.append(n)
    return names[:8]


def unload_idle_model(rules, probe, prev, status, http):
    """The owner's choice: inside the idle hours, when the ONLY thing stopping us is VRAM held by an idle Ollama
    model, and that has been so for `after_min` minutes (two ticks by default), ask the PC's Ollama to drop every loaded model
    (`keep_alive: 0`); the owner's next prompt reloads it (and the watcher then yields to it). Returns the names unloaded, [] when
    nothing was done. `status` gets `vram_blocked_since` while we wait and `unloaded` when we act."""
    cfg = (rules.get("pc_idle") or {}).get("unload_idle_model")
    if not isinstance(cfg, dict) or not isinstance(probe, dict):
        return []
    ov = C.pc_override()
    if C.pc_idle(rules, probe, override=ov)[0] or not C.pc_idle(rules, dict(probe, gpu_mem_used_mb=0), override=ov)[0]:
        return []                                                       # already idle, or something besides VRAM blocks us
    procs = {str(p).lower() for p in probe.get("processes") or [] if isinstance(p, str)}
    if "ollama.exe" not in procs:
        return []                                                       # VRAM held by something that is not Ollama
    since = prev.get("vram_blocked_since") if isinstance(prev.get("vram_blocked_since"), str) else None
    try:
        since_t = calendar.timegm(time.strptime(since, "%Y-%m-%dT%H:%M:%SZ")) if since else None
    except ValueError:
        since_t = None
    if since_t is None:
        since, since_t = C.now(), time.time()
    status["vram_blocked_since"] = since
    if time.time() - since_t < cfg.get("after_min", 10) * 60:
        return []
    url = cfg["ollama"]
    unloaded = []
    for name in ollama_loaded(url, http):
        if http("POST", url + "/api/generate", {"model": name, "keep_alive": 0}) is not None:
            unloaded.append(name)
    if unloaded:
        status["unloaded"] = unloaded
        C.log(f"unloaded idle Ollama model(s) for VRAM: {' '.join(unloaded)}")
    return unloaded


def placed_exists(mark, lang):
    """True when the mark records a finished placement for lang whose sidecar is still on disk: the item has our subtitle
    even if library.json (built from the nightly Plex copy) still lists the language as needed. A missing file (deleted by
    hand) counts as not placed, so the item is generated again."""
    placed = (mark or {}).get("placed")
    rec = placed.get(lang) if isinstance(placed, dict) else None
    if not isinstance(rec, dict) or rec.get("pending") or not isinstance(rec.get("path"), str):
        return False
    try:
        return os.path.isfile(rec["path"])
    except (OSError, ValueError):
        return False


def pick_item(library, rules, marks_all, decisions_all, busy_keys, requests):
    """The one item to generate: a 'generate' request first (return (key, lang, request_id)), else the best generatable item
    (English audio, 'en' in need, not excluded, not burned_in, gen_failed < 2, no image subtitle in the language, key+lang
    not in busy_keys, no placed[lang] mark whose file still exists unless a request asks) ordered by views desc, library priority, added_at desc. Returns (key, lang, request_id | None) or None."""
    items = library.get("items", {})
    prio = {name: i for i, name in enumerate(rules.get("priority", {}).get("libraries", []))}

    def ok(key, item, lang, from_request=False):
        if item.get("status") != "ok" or lang not in (item.get("need") or []) or C.excluded_library(item.get("library"), rules):
            return False
        if f"{key}.{lang}" in busy_keys or key in busy_keys:
            return False
        d = decisions_all.get(key)
        if d and d.get("decision") == "exclude":
            return False
        m = marks_all.get(key) or {}
        if m.get("burned_in") or int(m.get("gen_failed", 0)) >= 2:
            return False
        if not from_request and placed_exists(m, lang):
            return False                                                # library.json lags a placement by up to a day
        if any(sub.get("kind") == "image" and str(sub.get("lang") or "").startswith(lang.split("-")[0]) for sub in (item.get("subs") or [])):
            return False
        if lang == "en":
            if not item.get("audio"):
                return False                                                # nothing to transcribe
            if any(ENGLISH_TAG.match(str(sub.get("lang") or "")) for sub in (item.get("subs") or []) if isinstance(sub, dict)):
                return False                                                # it has English subs ('en-us' too), whatever 'need' says
            tags = [str(a.get("lang") or "").lower() for a in (item.get("audio") or []) if isinstance(a, dict)]
            tagged = [t for t in tags if t and t not in ("und", "unknown", "mis", "zxx")]
            if tagged and not any(t.startswith("en") for t in tagged):       # every tagged track is another language
                return False                                                # untagged audio (most cartoon rips) is allowed
        return True

    for rid, req in requests:
        if req.get("kind") != "generate":
            continue
        keys = [req["key"]] if isinstance(req.get("key"), str) else \
            [k for k, it in items.items() if str(it.get("show_plex_id")) == str(req.get("show")) or (it.get("kind") == "mv" and str(it.get("plex_id")) == str(req.get("show")))]
        for k in keys:
            it = items.get(k)
            if it:
                for lang in it.get("need") or ["en"]:
                    if ok(k, it, lang, from_request=True):
                        return k, lang, rid
    best = None
    for k, it in items.items():
        if not ok(k, it, "en"):
            continue
        views = sum(v for v in (it.get("views") or {}).values() if isinstance(v, int))
        rank = (-views, prio.get(it.get("library"), 99), -(it.get("added_at") or 0), k)
        if best is None or rank < best[0]:
            best = (rank, k)
    return (best[1], "en", None) if best else None


ENGLISH_TAG = re.compile(r"^(en|eng)(-|$)", re.I)


def english_audio_index(video, run):
    """Index (among the audio streams) of the first English-tagged audio track, else 0. Dual-audio files often put the
    Japanese or Spanish track first; Whisper must hear the English one, or a '.en' file comes out in that language."""
    rc, out, _ = run([C.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=language",
                      "-of", "csv=p=0", video], 60)
    if rc != 0:
        return 0
    langs = out.decode("utf-8", "replace").splitlines() if isinstance(out, (bytes, bytearray)) else str(out or "").splitlines()
    for i, lang in enumerate(langs[:32]):
        if ENGLISH_TAG.match(lang.strip().strip(",")):
            return i
    return 0


def extract(video, out_dir, rules, run):
    """audio.opus (mono 16 kHz 32 kb/s) and FRAMES jpegs of the bottom 25 % of the picture at 5..95 % of the duration into
    out_dir (C.ensure_dir). ffprobe gives the duration. Caps: AUDIO_CAP, FRAME_CAP each. Returns (ok, why, duration_ms)."""
    C.ensure_dir(out_dir)
    rc, out, err = run([C.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video], 60)
    try:
        secs = float(out.decode("utf-8", "replace").strip().splitlines()[0])
        duration_ms = int(secs * 1000) if math.isfinite(secs) and 0 < secs < 10 ** 6 else 0      # inf/nan/absurd -> failed
    except (ValueError, IndexError, OverflowError):
        duration_ms = 0
    if rc != 0 or duration_ms <= 0:
        return False, "ffprobe failed", 0
    audio = os.path.join(out_dir, "audio.opus")
    track = english_audio_index(video, run)
    rc, _, err = run([C.FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", video, "-map", f"0:a:{track}", "-vn", "-ac", "1",
                      "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", audio], FFMPEG_TIMEOUT)
    if rc != 0:
        return False, f"ffmpeg audio exit {rc}", duration_ms
    try:
        if os.lstat(audio).st_size > AUDIO_CAP:
            return False, "audio too big", duration_ms
    except OSError:
        return False, "no audio written", duration_ms
    frames_dir = os.path.join(out_dir, "frames")
    C.ensure_dir(frames_dir)
    good = 0
    for i in range(FRAMES):
        t = duration_ms / 1000.0 * (0.05 + 0.90 * i / (FRAMES - 1))
        out_jpg = os.path.join(frames_dir, f"{i:02d}.jpg")
        rc, _, err = run([C.FFMPEG, "-nostdin", "-loglevel", "error", "-threads", "2", "-y", "-ss", f"{t:.2f}", "-i", video,
                          "-frames:v", "1", "-vf", "crop=iw:ih/4:0:ih*3/4,scale=640:-2", "-q:v", "4", out_jpg], 60)
        if rc != 0 and i == 0:
            C.log(f"frame extraction: ffmpeg exit {rc}: {str(err).strip()[-200:]}")
        try:
            if rc == 0 and os.lstat(out_jpg).st_size == 0:
                rc = 1
        except OSError:
            rc = 1
        try:
            if rc != 0 or os.lstat(out_jpg).st_size > FRAME_CAP:
                try:
                    os.unlink(out_jpg)
                except OSError:
                    pass
            else:
                good += 1
        except OSError:
            pass
    if good < FRAMES // 2:                                  # the burned-in check needs frames to look at
        return False, f"frames failed ({good}/{FRAMES})", duration_ms
    return True, "", duration_ms


def validate_result(obj):
    """SECURITY.md "How it's fenced in": confidence finite -> clamped 0..1; no_speech_ratio finite 0..1; language_detected str <= 8;
    burned_in = {frames_with_text int, FRAMES//2 <= of <= FRAMES, 0 <= frames_with_text <= of}. Returns the cleaned dict or None."""
    if not isinstance(obj, dict):
        return None
    def fin(v, lo, hi):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi
    conf, nsr = obj.get("confidence"), obj.get("no_speech_ratio")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not math.isfinite(conf):
        return None
    if not fin(nsr, 0, 1):
        return None
    lang = obj.get("language_detected")
    if not isinstance(lang, str) or len(lang) > 8:
        return None
    bi = obj.get("burned_in")
    of = bi.get("of") if isinstance(bi, dict) else None
    if not isinstance(of, int) or isinstance(of, bool) or not FRAMES // 2 <= of <= FRAMES:
        return None                                                     # extract() ships 6..12 frames; the worker reports what it saw
    n = bi.get("frames_with_text")
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= of:
        return None
    return {"confidence": min(1.0, max(0.0, float(conf))), "no_speech_ratio": float(nsr), "language_detected": lang,
            "burned_in": {"frames_with_text": n, "of": FRAMES}}


def mark_failure(key, why):
    """C.mark(key, gen_failed_inc=1, last_failure=why[:200]); returns the new gen_failed count."""
    m = C.mark(key, gen_failed_inc=1, last_failure=str(why)[:200])
    return int(m.get("gen_failed", 0))


def kill_remote_worker(remote, run):
    """Stop our worker on the PC: every python process whose command line names this job folder."""
    esc = remote.replace("'", "''")
    run(ssh_args(f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{esc}*' -and $_.Name -like 'python*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"), SSH_TIMEOUT)


def _log_worker_tail(errlog):
    """Put the last 400 bytes of the worker's stderr in the journal (redacted); never raises."""
    try:
        errlog.flush()
        with open(errlog.name, "rb") as fh:
            fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 400))
            tail = fh.read().decode("utf-8", "replace").strip().replace("\n", " | ")
        if tail:
            C.log(f"worker said: {tail[-400:]}")
    except (OSError, AttributeError, ValueError):
        pass


def run_worker_watched(remote, run, rules=None, probe=probe_pc, poll_s=30):
    """Run the worker over SSH in the background and probe the PC every poll_s seconds; the moment another process works
    the GPU (Ollama, a game, anything but our own python worker) the worker is killed and (1, 'preempted: <name>') is
    returned, so the owner's model never waits. Returns (0, '') on success, (124, 'worker timed out') at PC_TIMEOUT,
    (rc, 'worker exit rc') otherwise."""
    import subprocess
    cmd = ssh_args(f'& "{C.PC_ROOT}\\venv\\Scripts\\python.exe" "{C.PC_ROOT}\\pc\\subs_worker.py" "{remote}"')
    errlog = None
    try:
        C.ensure_dir(C.PC_DIR)
        errlog = open(os.path.join(C.PC_DIR, "worker.err"), "wb")          # the worker's last words, for the journal on failure
    except OSError:
        errlog = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errlog or subprocess.DEVNULL)
    except OSError as exc:
        return 127, f"ssh failed: {exc.strerror}"
    deadline = time.monotonic() + PC_TIMEOUT
    while True:
        try:
            rc = proc.wait(timeout=poll_s)
            if rc != 0:
                _log_worker_tail(errlog)
            return (0, "") if rc == 0 else (rc, f"worker exit {rc}")
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() > deadline:
            proc.kill(); kill_remote_worker(remote, run)
            return 124, "worker timed out"
        p = probe(run)
        done_ms = read_progress(remote, run)
        if done_ms is not None:
            C.set_current(phase="transcribing", done_ms=done_ms)
        others = C.gpu_busy_others(p, ignore=("python.exe", "python3.exe", "pythonw.exe")) if p else []
        hard = [n for n in others if n.lower() in {x.lower() for x in ((rules or {}).get("pc_idle") or {}).get("yield_to", ["ollama.exe"])}] or others
        if hard:
            proc.kill(); kill_remote_worker(remote, run)
            return 1, f"preempted: {hard[0]}"


def read_progress(remote, run):
    """done_ms from the worker's progress.json on the PC, or None. Untrusted: 4 KiB cap, JSON object, int in range."""
    esc = remote.replace("'", "''")
    rc, out, _ = run(ssh_args(f"Get-Content -Raw -LiteralPath '{esc}\\progress.json'"), SSH_TIMEOUT)
    if rc != 0 or not out or len(out) > 4096:
        return None
    try:
        obj = json.loads(out.decode("utf-8", "replace").lstrip("\ufeff"))
    except ValueError:
        return None
    v = obj.get("done_ms") if isinstance(obj, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 10 ** 9:
        return None
    return int(v)


def run_pc(key, run, rules=None):
    if not C.ITEM_KEY.fullmatch(str(key)):
        return False, "bad key"
    """scp PC_DIR/<key> to <PC_ROOT>\\jobs\\<key>\\, run the worker over SSH (PC_TIMEOUT), check remote sizes
    (out.srt <= SRT_CAP, result.json <= RESULT_CAP, else delete remotely and fail), scp out.srt + result.json back into
    PC_DIR/<key>/, delete the remote folder (always, also on failure). Returns (ok, why)."""
    local = os.path.join(C.PC_DIR, key)
    remote = C.PC_ROOT + "\\jobs\\" + key
    remote_fwd = remote.replace("\\", "/")
    try:
        C.set_current(phase="uploading")
        rc, _, err = run(["scp", "-q", "-r", local, f"{C.PC}:{remote_fwd}"], 600)
        if rc != 0:
            return False, "copy to the PC failed"
        C.set_current(phase="transcribing", done_ms=0)
        rc, why = run_worker_watched(remote, run, rules)
        if rc != 0:
            return False, why
        rc, out, _ = run(ssh_args(f'(Get-Item "{remote}\\out.srt").Length; (Get-Item "{remote}\\result.json").Length'), SSH_TIMEOUT)
        try:
            sizes = [int(x) for x in out.decode("utf-8", "replace").split()]
        except ValueError:
            sizes = []
        if rc != 0 or len(sizes) != 2:
            return False, "no result on the PC"
        if sizes[0] > SRT_CAP or sizes[1] > RESULT_CAP:
            return False, "result too big"
        for name in ("out.srt", "result.json"):
            rc, _, _ = run(["scp", "-q", f"{C.PC}:{remote_fwd}/{name}", os.path.join(local, name)], 120)
            if rc != 0:
                return False, f"copy back failed ({name})"
        return True, ""
    finally:
        run(ssh_args(f'Remove-Item -Recurse -Force "{remote}"'), SSH_TIMEOUT)


def main():
    rules = C.load_rules()
    if rules is None:
        return 0
    override = C.pc_override()
    if not override and not C.inside_idle_hours(rules):                 # (runs every minute: no SSH to the PC outside the hours)
        prev = C.read_json(C.PC_STATUS, 8192) or {}
        C.write_json(C.PC_STATUS, {"at": C.now(), "idle": False, "why": "outside the idle hours", "gpu_util": None,
                                   "idle_since": None, "last_run": prev.get("last_run")})
        return 0
    probe = probe_pc(run)                                   # module-level `run` (real_run) so tests can patch it
    idle, why = C.pc_idle(rules, probe, override=override)
    prev = C.read_json(C.PC_STATUS, 8192) or {}
    status = {"at": C.now(), "idle": idle, "why": why + (" (generate now)" if override and idle else ""), "gpu_util": (probe or {}).get("gpu_util"),
              "idle_since": prev.get("idle_since") if idle and prev.get("idle") else (C.now() if idle else None),
              "last_run": prev.get("last_run")}
    if not idle and unload_idle_model(rules, probe, prev, status, real_http):
        time.sleep(5)                                                   # let the driver release the memory, then look again
        probe = probe_pc(run)
        idle, why = C.pc_idle(rules, probe, override=override)
        status.update({"idle": idle, "why": why + (" (generate now)" if override and idle else ""), "gpu_util": (probe or {}).get("gpu_util"), "idle_since": C.now() if idle else None})
    C.write_json(C.PC_STATUS, status)
    if not idle:
        return 0
    hours = C.read_json(C.GEN_HOURS, 4096) or {}
    night = C.night_id()
    spent = hours.get("seconds", 0) if hours.get("night") == night else 0
    if spent >= rules["generate"]["max_hours_per_night"] * 3600:
        return 0
    library = C.read_json(C.LIBRARY, 200 * 1024 * 1024) or {"items": {}}
    busy = set()
    for folder in (C.INCOMING, C.QUEUE, C.REVIEW):
        try:
            busy |= {n[:-4] for n in os.listdir(folder) if n.endswith(".srt")}
        except OSError:
            pass
    marks_all = {k: C.marks(k) for k in library.get("items", {})}
    decisions_all = {k: C.decision(k) for k in library.get("items", {})}
    picked = pick_item(library, rules, marks_all, decisions_all, busy, C.requests_pending())
    if not picked:
        return 0
    key, lang, request_id = picked
    if request_id:
        try:
            os.unlink(os.path.join(C.REQUESTS, request_id + ".json"))
        except OSError:
            pass
    item = library["items"][key]
    video = C.safe_media_path(item.get("path"), rules)
    if video is None:
        mark_failure(key, "video missing")
        C.done({"step": "gen", "key": key, "lang": lang, "result": "failed", "why": "video missing", **_meta(item)})
        return 0
    job_dir = os.path.join(C.PC_DIR, key)
    shutil.rmtree(job_dir, ignore_errors=True)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))                 # a stop still runs finally/atexit: current.json goes
    started = time.monotonic()
    C.set_current(step="gen", phase="extracting", key=key, lang=lang, started_at=C.now(), **_meta(item))
    try:
        ok, why, duration_ms = extract(video, job_dir, rules, run)
        timing = {"extract_s": int(time.monotonic() - started)}
        C.set_current(of_ms=duration_ms if isinstance(duration_ms, int) and duration_ms > 0 else None)
        if not ok:
            mark_failure(key, why); C.done({"step": "gen", "key": key, "lang": lang, "result": "failed", "why": why, **_meta(item)})
            return 0
        task = "transcribe" if lang == "en" else "translate"
        C.write_json(os.path.join(job_dir, "job.json"), {"key": key, "lang": lang, "task": task,
                                                          "audio_lang": (item.get("audio") or [{}])[0].get("lang"), "duration_ms": duration_ms})
        pc_started = time.monotonic()
        ok, why = run_pc(key, run, rules)
        timing["pc_s"] = int(time.monotonic() - pc_started)
        C.set_current(phase="checking")
        if not ok:
            if why.startswith("preempted"):
                C.mark(key, interrupted_at=C.now(), last_failure=why)      # not a failure: picked up again on the next idle tick
                C.done({"step": "gen", "key": key, "lang": lang, "result": "interrupted", "why": why, **_meta(item)})
            else:
                mark_failure(key, why); C.done({"step": "gen", "key": key, "lang": lang, "result": "failed", "why": why, **_meta(item)})
            return 0
        result = validate_result(C.read_json(os.path.join(job_dir, "result.json"), RESULT_CAP))
        if result is None:
            mark_failure(key, "bad result"); C.done({"step": "gen", "key": key, "lang": lang, "result": "failed", "why": "bad result", **_meta(item)})
            return 0
        bi = result["burned_in"]
        if bi["frames_with_text"] / bi["of"] >= rules["generate"]["burned_in_ratio"]:
            C.mark(key, burned_in=True)
            C.done({"step": "gen", "key": key, "lang": lang, "result": "burned_in", **_meta(item)})
            return 0
        raw = C.read_bytes(os.path.join(job_dir, "out.srt"), SRT_CAP)
        cues, why = C.validate_subtitle(raw, lang)
        if cues is None:
            mark_failure(key, why); C.done({"step": "gen", "key": key, "lang": lang, "result": "failed", "why": why, **_meta(item)})
            return 0
        source = f"gen:{rules['generate']['model']}"
        C.write_bytes(os.path.join(C.INCOMING, f"{key}.{lang}.srt"), C.write_srt(cues, (key, source, result["confidence"])))
        C.write_json(os.path.join(C.INCOMING, f"{key}.{lang}.json"), {"key": key, "lang": lang, "source": source,
                     "confidence": result["confidence"], "no_speech_ratio": result["no_speech_ratio"], "at": C.now(), "step": "gen"})
        C.done({"step": "gen", "key": key, "lang": lang, "result": "queued", "source": source, "confidence": result["confidence"], **timing, **_meta(item)})
    finally:
        C.clear_current()
        shutil.rmtree(job_dir, ignore_errors=True)
        seconds = int(time.monotonic() - started)
        C.write_json(C.GEN_HOURS, {"night": night, "seconds": spent + seconds})
        status["last_run"] = C.now()
        C.write_json(C.PC_STATUS, status)
    return 0


def _meta(item):
    return {k: item.get(k) for k in ("show", "title", "season", "episode", "library")}


if __name__ == "__main__":
    sys.exit(main())
