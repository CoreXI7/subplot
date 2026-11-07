import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path[:0] = [_os.path.join(_ROOT, "subplot"), _os.path.join(_ROOT, "pc"), _os.path.join(_ROOT, "tests")]   # the code lives in subplot/ and pc/
import os, sys, tempfile

try: TMP = tempfile.mkdtemp(dir=os.environ.get("SUBS_SANDBOX", "/tmp"))
except Exception: TMP = tempfile.mkdtemp()
HOME, STATE = os.path.join(TMP, "home"), os.path.join(TMP, "state")
for _d in (HOME, STATE):
    os.makedirs(_d, mode=0o700, exist_ok=True)
os.environ.update({"SUBS_HOME": HOME, "SUBS_STATE": STATE, "SUBS_FFMPEG": "/bin/false",
                   "SUBS_RULES": os.path.join(TMP, "rules.json"),
                   "SUBS_FFPROBE": "/bin/false"})
_CODE = os.environ.get("SUBS_CODE", "")
if _CODE:
    sys.path.insert(0, _CODE)
try:
    import common as C
except Exception as _e:
    print("FAIL import_common: %s" % type(_e).__name__)
    print("SUMMARY pass=0 fail=1")
    sys.exit(0)
MAXB = int(getattr(C, "MAX_SUB_BYTES", 2 * 1024 * 1024))
MAXC = int(getattr(C, "MAX_CUES", 20000))
MAXMS = int(getattr(C, "MAX_CUE_MS", 60000))

def txt(x):
    return x if isinstance(x, str) else bytes(x).decode("utf-8", "replace") if isinstance(x, (bytes, bytearray)) else str(x)

def ts(ms, sep=","):
    sign, ms = ("-", -ms) if ms < 0 else ("", ms)
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return "%s%02d:%02d:%02d%s%03d" % (sign, h, m, s, sep, ms)

def srt(cues, sep=",", eol="\n", bom=b""):
    out = []
    for i, (a, b, t) in enumerate(cues):
        out += [str(i + 1), "%s --> %s" % (ts(a, sep), ts(b, sep)), t, ""]
    return bom + eol.join(out).encode()

def leaks(s):
    return "/media/" in s.lower() or "hentai" in s.lower()

def bad_reason(why):
    if not isinstance(why, str) or not why.strip():
        return "no short reason code"
    if len(why) > 120 or "\n" in why or "\r" in why or leaks(why):
        return "reason not short: %r" % why[:60]
    return None

def bad_cues(cues, why):
    if cues is None:
        return bad_reason(why)
    if not isinstance(cues, (list, tuple)):
        return "cues is %s" % type(cues).__name__
    if len(cues) > MAXC:
        return "too many cues"
    for i, cue in enumerate(cues):
        if not isinstance(cue, (list, tuple)) or len(cue) != 3:
            return "cue %d malformed" % i
        a, b, t = cue
        if not isinstance(a, int) or not isinstance(b, int) or a < 0 or b < 0:
            return "cue %d bad time" % i
        if a > b:
            return "cue %d start>end" % i
        if b - a > MAXMS:
            return "cue %d over cap" % i
        if not isinstance(t, str):
            return "cue %d text not str" % i
        if "\x00" in t:
            return "cue %d NUL in text" % i
    return None

def validate(raw, lang="en"):
    res = C.validate_subtitle(raw, lang)
    assert isinstance(res, (list, tuple)) and len(res) == 2, "validate_subtitle -> %r" % (res,)
    cues, why = res
    bad = bad_cues(cues, why)
    assert bad is None, bad
    return cues, why

def parse(raw):
    cues = C.parse_srt(raw)
    bad = bad_cues(cues, None)
    assert cues is None or bad is None, bad
    return cues

def refused(raw, lang="en"):
    cues, why = validate(raw, lang)
    assert cues is None, "accepted instead of refusing"
    assert not (len(why) >= 12 and why in txt(raw)), "reason echoes input"
    return why

def vok(raw, lang="en"):
    cues = validate(raw, lang)[0]
    assert cues is not None, "refused a valid file"
    return cues

def empty_ok(raw, lang="en"):
    cues = validate(raw, lang)[0]
    assert cues is None or len(cues) == 0, "expected no cues"

def must(cond, msg): assert cond, msg

def marker_text(k, source="PLEX", conf=0.9):
    return f"; subplot {k} {source} {float(conf):.4f}"            # built by hand: write_srt refuses bad parts on purpose

def key():
    try:
        return C.item_key("movie", 123456)
    except Exception:
        return "movie:123456"

def marker_of(text):
    got = C.our_marker(text)
    if got is not None:
        assert isinstance(got, (list, tuple)) and len(got) == 3, "our_marker -> %r" % (got,)
    return got

def strip(text):
    fn = getattr(C, "strip_marker", None)
    assert callable(fn), "strip_marker missing"
    out = fn(text)
    return txt(out[0] if isinstance(out, (list, tuple)) else out)

CASES = []
def add(name, fn): CASES.append((name, fn))

def oversize():
    d = srt([(1000, 2000, "A")]); refused(d + b"\n" * (MAXB + 1 - len(d)))

def numberless(): return parse(b"00:00:01,000 --> 00:00:02,000\nA\n\n00:00:03,000 --> 00:00:04,000\nB\n")

def conf_case(c):
    try: line = marker_text(key(), "PLEX", c)
    except C.SubtitleError: return
    got = marker_of(line)
    if got is not None:
        must(0.0 <= float(got[2]) <= 1.0, "confidence %r accepted" % c)

def injection():
    # the real writer must refuse a key or source that could smuggle a path or a newline into the marker line
    for k, src in (("evil\n/media/x", "PLEX"), (key(), "Hentai /media/x"), (key(), "a\nb")):
        try:
            out = C.write_srt([(0, 1000, "x")], (k, src, 0.55))
        except (C.SubtitleError, ValueError):
            continue
        must(not leaks(txt(out)), "marker echoes banned path or word")

def write_checked(cues):
    try: out = C.write_srt(cues, (key(), "PLEX", 0.8))
    except (C.SubtitleError, ValueError): return          # refusing to write a cue the parser would reject is the fix
    bad = bad_cues(C.parse_srt(out), None)
    must(bad is None, "wrote a bad cue: %s" % bad)

def roundtrip():
    cues = [(1000, 2000, "A"), (3000, 4200, "B")]; out = C.write_srt(cues, (key(), "PLEX", 0.8))
    must(marker_of(txt(out)) is not None, "marker not first line")
    back = C.parse_srt(out)
    must(back is not None and [(a, b, t) for a, b, t in back] == cues, "roundtrip mismatch")

ASS = ("[Script Info]\nTitle: t\n\n[Events]\n"
       "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
       "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Hello, world, again\n"
       "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\\i1}Italic{\\i0} text\\Nsecond\n")

def ass_commas():
    cues = parse(C.ass_to_srt(ASS))
    assert cues is not None and len(cues) == 2 and cues[0][:2] == (1000, 2500)
    must("Hello, world, again" in cues[0][2], "commas in dialogue text split wrong")

def ass_odd():
    odd = "[Script Info]\nTitle: odd\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00\nComment: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,x\n[Unknown]\nfoo\n"
    try: out = C.ass_to_srt(odd)
    except C.SubtitleError: return
    bad = bad_cues(C.parse_srt(out), None)
    must(bad is None, bad)

def other_key_first():
    # a marker whose key is not an item key (ep-N / mv-N) is not ours: it must never let place replace that file
    k = key() + ":o"; got = marker_of(marker_text(k, "MANUAL", 0.5))
    must(got is None, "a marker with an invalid key was accepted")

add("oversized_2mib", oversize)
add("too_many_cues", lambda: validate(srt([(i * 10, i * 10 + 5, "x") for i in range(MAXC + 1)])))
add("backwards", lambda: refused(srt([(9000, 1000, "back")])))
add("negative_time", lambda: refused(b"1\n-00:00:01,000 --> 00:00:02,000\nneg\n"))
add("missing_indexes", lambda: must((lambda c: c is not None and len(c) == 2)(numberless()), "numberless cues not parsed"))
for _n, _c in (("marker_conf_high", 1.5), ("marker_conf_negative", -1), ("marker_conf_long", 0.12345)):
    add(_n, lambda c=_c: conf_case(c))
add("marker_injection", injection)
add("two_marker_lines", lambda: must((lambda l: l not in strip(l + "\n" + l))(marker_text(key())), "duplicate markers not stripped"))
add("write_roundtrip", roundtrip)
add("write_backwards", lambda: write_checked([(5000, 1000, "x")]))
add("write_huge_duration", lambda: write_checked([(0, 600000000, "x")]))
add("ass_commas", ass_commas)
add("ass_odd_events", ass_odd)
add("ass_via_validate", lambda: must((lambda c: len(c) == 2 and "Hello, world, again" in c[0][2])(vok(ASS.encode())), "ASS not converted"))
add("other_key_first", other_key_first)

for _n, _f in (
    ("cue_61s", lambda: validate(srt([(0, 61000, "long")]))),
    ("overlap", lambda: validate(srt([(0, 5000, "a"), (3000, 8000, "b")]))),
    ("hundred_hours", lambda: validate(b"1\n100:00:00,000 --> 100:00:01,000\nfar\n")),
    ("dot_sep", lambda: validate(srt([(1000, 2500, "d")], sep="."))),
    ("comma_sep", lambda: vok(srt([(1000, 2500, "c")]))),
    ("crlf", lambda: vok(srt([(1000, 2000, "A"), (3000, 4000, "B")], eol="\r\n"))),
    ("utf8_bom", lambda: vok(b"\xef\xbb\xbf" + srt([(1000, 2000, "b")]))),
    ("utf16", lambda: validate(srt([(1000, 2000, "w")]).decode().encode("utf-16"))),
    ("latin1", lambda: validate(b"1\n00:00:01,000 --> 00:00:02,000\ncaf\xe9\n")),
    ("nul_controls", lambda: validate(srt([(1000, 2000, "A\x00B\x01C\x7f")]))),
    ("nested_tags", lambda: validate(srt([(1000, 2000, "<i><b>x<i>y</b>")]))),
    ("script_font", lambda: validate(srt([(1000, 2000, "<script>x</script><font color=red>y</font>")]))),
    ("entities", lambda: validate(srt([(1000, 2000, "&lt;b&gt; &amp; &#233;")]))),
    ("text_100k", lambda: validate(srt([(1000, 2000, "A" * 100000)]))),
    ("binary", lambda: validate(bytes(range(256)) * 4)),
    ("ass_garbage", lambda: validate(b"[Events]\nDialogue: garbage\n", "en")),
    ("blank_only", lambda: empty_ok(b"\n\n   \n\t\n")),
    ("marker_only", lambda: empty_ok(marker_text(key()))),
    ("newlines_1m", lambda: empty_ok(b"\n" * 1000000)),
    ("empty", lambda: refused(b"")),
    ("our_marker_first", lambda: must(marker_of(marker_text(key(), "PLEX", 0.75)) is not None, "first marker rejected")),
    ("our_marker_middle", lambda: must(marker_of("x\n" + marker_text(key()) + "\ny") is None, "middle marker accepted")),
    ("strip_first", lambda: must(marker_text(key()) not in strip(marker_text(key()) + "\nx"), "first marker kept")),
    ("strip_last", lambda: must(marker_text(key()) not in strip("x\n" + marker_text(key())), "last marker kept")),
    ("strip_middle", lambda: must(marker_text(key()) in strip("x\n" + marker_text(key()) + "\ny"), "middle marker stripped")),
    ("strip_both", lambda: must(marker_text(key()) not in strip(marker_text(key()) + "\nx\n" + marker_text(key())), "end markers kept")),
    ("other_key_last", lambda: must(marker_of("x\n" + marker_text(key() + ":o")) is None, "trailing marker accepted")),
):
    add(_n, _f)

def main():
    p = f = 0
    for name, fn in CASES:
        try:
            fn()
            p += 1
            print("PASS %s" % name)
        except AssertionError as e:
            f += 1; print("FAIL %s: %s" % (name, (str(e) or "assertion")[:200]))
        except Exception as e:
            f += 1; print("FAIL %s: unhandled %s: %s" % (name, type(e).__name__, str(e)[:160]))
    print("SUMMARY pass=%d fail=%d" % (p, f))
    return 0

if __name__ == "__main__":
    sys.exit(main())
