#!/usr/bin/env python3
"""Break-it runner: the PC is hostile. Probe JSON, progress.json, result.json, Ollama answers and remote paths."""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc"), _os.path.join(_ROOT, "tests")]   # the code lives in subplot/ and pc/
import json, math, os, sys, tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SBX = tempfile.mkdtemp(prefix="pc-attack-", dir=os.environ.get("SUBS_SANDBOX", os.environ.get("SUBS_SANDBOX", "")))
os.environ.update(SUBS_HOME=SBX, SUBS_STATE=os.path.join(SBX, "state"), SUBS_RULES=os.path.join(SBX, "rules.json"),
                  SUBS_FFMPEG="/bin/false", SUBS_FFPROBE="/bin/false")
os.makedirs(os.environ["SUBS_STATE"], mode=0o700, exist_ok=True)
sys.path.insert(0, HERE)
import common as C   # noqa: E402
import gen           # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else ": " + str(detail)[:160]))


def safe(name, fn, want=None):
    """fn must not raise; when want is given the result must equal it."""
    try:
        got = fn()
    except Exception as exc:
        check(name, False, f"raised {type(exc).__name__}: {exc}")
        return None
    check(name, True if want is None else got == want, got)
    return got


RULES = {"pc_idle": {"hours": ["00:00-23:59"], "no_processes": ["game.exe"], "not_games": ["wallpaper64.exe"], "gpu_util_below": 20,
                     "vram_free_mb": 6000, "yield_to": ["ollama.exe"], "unload_idle_model": {"ollama": "http://10.0.0.50:11434", "after_min": 0}}}
GOOD = {"gpu_util": 2, "gpu_mem_used_mb": 1000, "gpu_mem_total_mb": 32000, "busy_procs": [], "processes": ["explorer.exe"], "game_procs": []}
check("good-probe-idle", C.pc_idle(RULES, GOOD)[0])

# ---- hostile probes
deep = {"a": None}
cur = deep
for _ in range(1000):
    cur["a"] = {"a": None}; cur = cur["a"]
safe("probe-1000-deep-blocks", lambda: C.pc_idle(RULES, {**GOOD, "processes": deep})[0], False)
safe("probe-1e5-names", lambda: C.pc_idle(RULES, {**GOOD, "processes": ["x.exe"] * 100000})[0], True)
for bad in ("x\ny.exe", "a'b.exe", "n\x00ul.exe", "l" * 200000):
    safe(f"probe-name-{bad[:6]!r}-no-crash", lambda b=bad: C.pc_idle(RULES, {**GOOD, "processes": [b], "game_procs": [b]})[0], False)
for bad in ("20", float("nan"), float("inf"), -1, 1e308, True, None, [], {}):
    safe(f"probe-gpu-util-{str(bad)[:6]}-blocks", lambda b=bad: C.pc_idle(RULES, {**GOOD, "gpu_util": b})[0], False)
for field in ("gpu_util", "gpu_mem_used_mb", "gpu_mem_total_mb", "busy_procs", "processes", "game_procs"):
    p = dict(GOOD); del p[field]
    safe(f"probe-missing-{field}-blocks", lambda p=p: C.pc_idle(RULES, p)[0], False)
safe("probe-our-python-odd-case-ignored", lambda: C.pc_idle(RULES, {**GOOD, "busy_procs": ["PYTHON.EXE"]}, ignore=("python.exe",))[0], True)
safe("probe-wallpaper-mixed-case-exempt", lambda: C.pc_idle(RULES, {**GOOD, "game_procs": ["Wallpaper64.EXE"]})[0], True)
safe("probe-wallpaper-plus-game-blocks", lambda: C.pc_idle(RULES, {**GOOD, "game_procs": ["Wallpaper64.EXE", "PEAK.exe"]})[0], False)
safe("probe-not-a-dict", lambda: C.pc_idle(RULES, ["x"])[0], False)
safe("probe-string", lambda: C.pc_idle(RULES, "idle")[0], False)

# ---- hostile progress.json
def runner(payload, rc=0):
    def run(args, timeout):
        return rc, payload if isinstance(payload, bytes) else json.dumps(payload).encode(), ""
    return run
for payload, want in (({"done_ms": 1e12}, None), ({"done_ms": -1}, None), ({"done_ms": "7"}, None), ({"done_ms": 5.9}, 5),
                      ({"done_ms": {"a": 1}}, None), ({"done_ms": True}, None), (b"x" * 4097, None), (b"\xff\xfe\x00", None),
                      ([1, 2], None), ({"done_ms": 1234}, 1234)):
    safe(f"progress-{str(payload)[:18]!r}", lambda p=payload: gen.read_progress("C:\\Subplot\\jobs\\ep-1", runner(p)), want)
safe("progress-rc1-none", lambda: gen.read_progress("C:\\Subplot\\jobs\\ep-1", runner({"done_ms": 1}, rc=1)), None)

# ---- hostile result.json
base = {"confidence": 0.9, "no_speech_ratio": 0.1, "language_detected": "en", "burned_in": {"frames_with_text": 1, "of": 12}}
check("result-good-accepted", gen.validate_result(base) is not None)
for patch in ({"confidence": float("nan")}, {"confidence": 2}, {"confidence": "0.9"}, {"no_speech_ratio": -0.1}, {"no_speech_ratio": float("inf")},
              {"burned_in": {"frames_with_text": 1, "of": 13}}, {"burned_in": {"frames_with_text": 1, "of": 5}}, {"burned_in": {"frames_with_text": 7, "of": 6}},
              {"burned_in": "x"}, {"burned_in": {"frames_with_text": True, "of": 12}}, {"language_detected": "e" * 9}, {"language_detected": 5}):
    obj = {**base, **patch}
    want_none = not (patch == {"confidence": 2})               # confidence 2 is clamped per spec; everything else refused
    got = safe(f"result-{str(patch)[:30]}", lambda o=obj: gen.validate_result(o))
    if got is not None or want_none:
        check(f"result-{str(patch)[:30]}-verdict", (got is None) == want_none, got)
big = {**base, **{f"k{i}": "x" * 1000 for i in range(1000)}}
safe("result-1MB-extra-keys", lambda: gen.validate_result(big) is not None, True)

# ---- hostile Ollama
def http_from(payload):
    def http(method, url, body=None):
        return payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return http
for payload, want in (({"models": [{"name": "llama3:8b x"}]}, []), ({"models": [{"name": "a;rm -rf /"}]}, []), ({"models": [{"name": "http://evil/x"}]}, []),
                      ({"models": [{"name": "../../etc"}]}, []), ({"models": [{"name": f"m{i}"} for i in range(9)]}, [f"m{i}" for i in range(8)]),
                      (b"x" * 70000, []), (b"not json", []), ({"models": "x"}, []), ({"models": [{"name": "ok-model:latest"}]}, ["ok-model:latest"])):
    safe(f"ollama-{str(payload)[:24]!r}", lambda p=payload: gen.ollama_loaded("http://10.0.0.50:11434", http_from(p)), want)
posts = []
def http_rec(method, url, body=None):
    posts.append((method, url, body))
    return json.dumps({"models": [{"name": "m1"}]}).encode() if url.endswith("/api/ps") else b"{}"
held = {**GOOD, "gpu_mem_used_mb": 31000, "gpu_mem_total_mb": 32600, "processes": ["ollama.exe"]}
status = {}
safe("unload-with-after-min-0", lambda: gen.unload_idle_model(RULES, held, {"vram_blocked_since": "2000-01-01T00:00:00Z"}, status, http_rec), ["m1"])
check("unload-only-configured-url", all(u.startswith("http://10.0.0.50:11434/") for _, u, _ in posts), posts)
posts.clear()
safe("unload-nothing-when-no-ollama", lambda: gen.unload_idle_model(RULES, {**held, "processes": ["lmstudio.exe"]}, {"vram_blocked_since": "2000-01-01T00:00:00Z"}, {}, http_rec), [])
check("unload-no-http-when-no-ollama", posts == [], posts)

# ---- remote paths in PowerShell / scp commands
calls = []
def rec(args, timeout):
    calls.append(args); return 0, b"", ""
for evil in ("C:\\Subplot\\jobs\\ep-1' ; Remove-Item -Recurse E:\\ ; '", "C:\\Subplot\\jobs\\$(whoami)", "C:\\Subplot\\jobs\\`whoami`", "C:\\Subplot\\jobs\\a && b", "C:\\Subplot\\jobs\\x\ny", "C:\\Subplot\\jobs\\日本"):
    calls.clear()
    safe(f"kill-remote-{evil[14:22]!r}-no-raise", lambda e=evil: gen.kill_remote_worker(e, rec))
    cmd = calls[-1][-1] if calls else ""
    quoted = evil.replace("'", "''")
    check(f"kill-remote-{evil[14:22]!r}-quoted", quoted in cmd and "\n" not in cmd.replace(evil, ""), cmd[:120])
for key in ("ep-1' ; rm", "ep-1$(x)", "*", "ep-1\n", "../ep-1"):
    safe(f"run_pc-bad-key-{key[:6]!r}-refused", lambda k=key: gen.run_pc(k, rec, RULES), (False, "bad key"))
check("ssh-args-shape", gen.ssh_args("Get-Date")[:2] == ["ssh", "-n"] and "-o" in gen.ssh_args("x"))
print(f"SUMMARY pass={sum(results)} fail={len(results) - sum(results)}")
