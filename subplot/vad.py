#!/usr/bin/env python3
"""Subplot: speech detection and timing measurement for sync.py (no network, no secrets; media read-only). ARCHITECTURE.md "sync.py".
Speech spans come from the Silero VAD model (models/silero_vad.onnx, pinned by sha256) run with onnxruntime on the CPU over
the WHOLE audio track, which ffmpeg decodes to 16 kHz mono PCM through a pipe (no temp file, bounded time and length).
The offset between a subtitle and the speech is the lag that maximises the cross-correlation of the two 10 ms on/off
signals, measured on each half of the file separately; the measurement is trusted only when both halves agree within
AGREE_MS. On 8 embedded, in-sync subtitles (shows, cartoons, anime, movies) it measured 7 within 250 ms and refused the
one whose subtitle drifts; a planted 20 s shift was recovered or refused, never misplaced. Audio-only aligners tried
first got half of them wrong.
numpy and onnxruntime live in the sync venv (VENV in subplot.conf), the interpreter of the sync service; when they or the model are
missing, load() raises Unavailable and sync leaves incoming/ alone for a later run."""
import hashlib, os, select, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

MODEL = os.path.join(C.HERE, "models", "silero_vad.onnx")
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"   # snakers4/silero-vad v5 (MIT)
RATE, WINDOW, CONTEXT = 16000, 512, 64            # Silero v5: 512-sample windows at 16 kHz, 64 samples of context before each
FRAME_MS = WINDOW * 1000 / RATE                   # 32 ms per probability
RES_MS = 10                                       # resolution of the on/off signals
THRESHOLD, MIN_SPEECH_MS, MIN_SILENCE_MS, PAD_MS = 0.5, 200, 300, 60
SEARCH_MS = 120000                                # lags searched: ±2 min (auto_shift_max is 60 s; beyond it a file is discarded anyway)
AGREE_MS = 1000                                   # the two regions must agree within this
MIN_Z = 2.5                                       # and each peak must stand this far above the median (random speech can agree by chance)
MIN_CUES_PER_HALF = 10                            # per measured region
WINDOW_MS = 6 * 60 * 1000                         # a long track is measured on two 6-min windows (25 % and 75 %), not read whole
WHOLE_FILE_MAX_MS = 2 * WINDOW_MS + 2 * SEARCH_MS # up to 16 min the whole track is decoded and its two halves measured
MIN_AUDIO_MS = 60000
MAX_AUDIO_S = 5 * 3600
MAX_MODEL_BYTES = 8 * 1024 * 1024
DECODE_TIMEOUT_S = 900
THREADS = 2

_session = None


class Unavailable(RuntimeError):
    """numpy, onnxruntime or the model are missing: no check can run (sync leaves incoming/ for a later run)."""


class SpeechError(RuntimeError):
    """ffmpeg failed or the audio is unusable for this one file (a non-timing failure: the owner looks)."""


def load():
    """The onnxruntime session (cached). Raises Unavailable when a module or the pinned model is missing."""
    global _session
    if _session is not None:
        return _session
    try:
        import numpy  # noqa: F401
        import onnxruntime as ort
    except ImportError as exc:
        raise Unavailable(f"missing module {getattr(exc, 'name', None) or exc}") from None
    try:
        if os.stat(MODEL).st_size > MAX_MODEL_BYTES:
            raise Unavailable("model file too big")
        with open(MODEL, "rb") as f:
            digest = hashlib.sha256(f.read(MAX_MODEL_BYTES + 1)).hexdigest()      # the whole file: a padded copy fails the pin
    except OSError as exc:
        raise Unavailable(f"model missing ({exc.strerror})") from None
    if digest != MODEL_SHA256:
        raise Unavailable("model sha256 mismatch")
    try:
        ort.disable_telemetry_events()                                    # no telemetry (the unit has no network anyway, but no noise either)
    except Exception:
        pass
    options = ort.SessionOptions()
    options.intra_op_num_threads = THREADS
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    try:
        _session = ort.InferenceSession(MODEL, options, providers=["CPUExecutionProvider"])
    except Exception as exc:                                              # onnxruntime raises its own hierarchy
        raise Unavailable(f"onnxruntime {type(exc).__name__}") from None
    return _session


def regions_for(duration_ms):
    """The two regions measured: the halves of a track up to WHOLE_FILE_MAX_MS, else a WINDOW_MS window centred at 25 % and
    one at 75 % (reads about 12 min of any file instead of all of it: the media drives also serve Plex)."""
    duration_ms = int(duration_ms or 0)
    if duration_ms <= WHOLE_FILE_MAX_MS:
        mid = duration_ms // 2
        return [(0, mid), (mid, duration_ms)]
    out = []
    for frac in (0.25, 0.75):
        lo = max(0, int(duration_ms * frac - WINDOW_MS / 2))
        out.append((lo, min(duration_ms, lo + WINDOW_MS)))
    return out


def speech_spans(video, duration_ms=None):
    """([(start_ms, end_ms), ...], duration_ms, regions) of speech in the video's default audio track. A track whose known
    duration (Plex's) is above WHOLE_FILE_MAX_MS is decoded only inside regions_for's two windows (ffmpeg -ss/-t); without a
    known duration, or for a short track, the whole track is decoded and its halves are the regions. ffmpeg writes 16 kHz mono
    PCM to a pipe (-threads 2, one DECODE_TIMEOUT_S wall clock for everything, MAX_AUDIO_S of audio); each 512-sample window
    goes through Silero with the 64 samples before it as context. Raises SpeechError (ffmpeg exit, timeout, too long, too
    short, no audio) or Unavailable."""
    session = load()
    deadline = time.monotonic() + DECODE_TIMEOUT_S
    known = int(duration_ms or 0)
    if known <= WHOLE_FILE_MAX_MS:
        probs = _decode_probs(session, video, None, deadline)
        measured_ms = int(len(probs) * FRAME_MS)
        if measured_ms < MIN_AUDIO_MS:
            raise SpeechError("audio too short" if probs else "no audio")
        return spans_from_probs(probs), measured_ms, regions_for(measured_ms)
    regions = regions_for(known)
    spans = []
    for lo, hi in regions:
        probs = _decode_probs(session, video, (lo, hi), deadline)
        if int(len(probs) * FRAME_MS) < MIN_AUDIO_MS:
            raise SpeechError("audio too short" if probs else "no audio")      # the file ends before Plex's duration says
        spans.extend((a + lo, b + lo) for a, b in spans_from_probs(probs))
    return spans, known, regions


def _decode_probs(session, video, region, deadline):
    """Silero probabilities (one per 32 ms) for the whole track, or for region (lo_ms, hi_ms) via ffmpeg -ss/-t."""
    import numpy as np
    seek = ["-ss", f"{region[0] / 1000:.3f}", "-t", f"{(region[1] - region[0]) / 1000:.3f}"] if region else []
    args = [C.FFMPEG, "-nostdin", "-loglevel", "error", "-threads", "2", *seek, "-i", video, "-vn", "-ac", "1", "-ar", str(RATE),
            "-f", "s16le", "-"]
    probs = []
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(RATE, dtype=np.int64)
    ctx = np.zeros(CONTEXT, dtype=np.float32)
    deadline = time.monotonic() + DECODE_TIMEOUT_S
    max_bytes = MAX_AUDIO_S * RATE * 2
    got, rest = 0, b""
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise SpeechError(f"ffmpeg: {exc.strerror or 'cannot start'}") from None
    try:
        fd = proc.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], min(remaining, 30.0))[0]:
                if remaining <= 0:
                    raise SpeechError("audio decode timed out")              # a stalled ffmpeg never blocks read() for good
                continue
            chunk = os.read(fd, WINDOW * 2 * 64)
            if not chunk:
                break
            got += len(chunk)
            if got > max_bytes:
                raise SpeechError("audio too long")
            data = rest + chunk
            n = len(data) // (WINDOW * 2)
            rest = data[n * WINDOW * 2:]
            if not n:
                continue
            x = np.frombuffer(data[:n * WINDOW * 2], dtype=np.int16).astype(np.float32).reshape(n, WINDOW) / 32768.0
            for i in range(n):
                out, state = session.run(None, {"input": np.concatenate([ctx, x[i]])[None, :], "state": state, "sr": sr})
                ctx = x[i][-CONTEXT:]
                probs.append(float(out[0, 0]))
        rc = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
        raise SpeechError("ffmpeg did not exit") from None
    except BaseException:                                                  # SpeechError or anything else: never leave a child behind
        proc.kill()
        proc.wait(timeout=30)
        raise
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
    if rc != 0:
        raise SpeechError(f"ffmpeg exit {rc}")
    return probs


def spans_from_probs(probs, threshold=THRESHOLD):
    """Speech spans in ms from per-window probabilities: windows >= threshold, gaps under MIN_SILENCE_MS merged, spans under
    MIN_SPEECH_MS dropped, PAD_MS added at both ends."""
    spans, start = [], None
    for i, p in enumerate(probs):
        if p >= threshold and start is None:
            start = i
        elif p < threshold and start is not None:
            spans.append([start * FRAME_MS, i * FRAME_MS])
            start = None
    if start is not None:
        spans.append([start * FRAME_MS, len(probs) * FRAME_MS])
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] < MIN_SILENCE_MS:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(int(max(0, a - PAD_MS)), int(b + PAD_MS)) for a, b in merged if b - a >= MIN_SPEECH_MS]


def _signal(np, spans, duration_ms, lo=0, hi=None):
    hi = duration_ms if hi is None else hi
    n = int(duration_ms // RES_MS) + 1
    s = np.zeros(n, dtype=np.float32)
    for a, b in spans:
        a, b = max(a, lo), min(b, hi)
        if b > a:
            s[int(a // RES_MS):min(n, int(-(-b // RES_MS)))] = 1.0        # half-open: (0, 10) is one 10 ms bin
    return s


def _best_lag(np, sub, voice):
    """(lag_ms, z): the lag within ±SEARCH_MS that maximises the normalised cross-correlation of the two signals; positive
    means the cues must move later. z = how far the peak stands above the median of the searched lags, in standard
    deviations (logged, not a gate). None when the signals are flat or the peak sits on the search edge."""
    a = sub - sub.mean()
    b = voice - voice.mean()
    norm = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if norm <= 0:
        return None, 0.0
    n = len(voice)
    m = min(int(SEARCH_MS // RES_MS), n - 1)                                 # a short file cannot be searched past its own length
    if m < 1:
        return None, 0.0
    size = 1 << (n + m).bit_length()                                         # no wrap-around between positive and negative lags
    cc = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size) / norm
    lags = np.concatenate([np.arange(0, m + 1), np.arange(-m, 0)])
    vals = np.concatenate([cc[:m + 1], cc[size - m:]])
    i = int(np.argmax(vals))
    lag = int(lags[i])
    if abs(lag) >= m or vals[m] >= vals[i] or vals[m + 1] >= vals[i]:      # the peak, or a tie with it, sits on a search edge
        return None, 0.0
    std = float(vals.std()) or 1.0
    return lag * RES_MS, round(float((vals[i] - np.median(vals)) / std), 1)


def measure(cues, spans, duration_ms, regions=None):
    """{"offset_ms": int | None, "halves": [h1, h2], "z": [z1, z2], "why": ""} for cues [(start_ms, end_ms, text), ...]
    against speech spans. Each of the two regions (regions_for(duration_ms) by default: the halves, or the two windows of a
    long track) is measured on its own (cues that start in it, all cues as the subtitle signal so a shifted cue from outside
    may belong in it); offset = the mean of the two when they agree within AGREE_MS and both peaks reach MIN_Z, else None with
    why "halves disagree", "too few cues", "weak peak" or "no peak". Positive = the cues must move later."""
    import numpy as np
    result = {"offset_ms": None, "halves": [None, None], "z": [None, None], "why": ""}
    if not cues:
        result["why"] = "too few cues"
        return result
    if not spans or not duration_ms or duration_ms <= 0:
        result["why"] = "no peak"
        return result
    sub = _signal(np, [(a, b) for a, b, _ in cues], duration_ms)
    voice = _signal(np, spans, duration_ms)
    margin = int(SEARCH_MS // RES_MS)
    regions = regions if isinstance(regions, (list, tuple)) and len(regions) == 2 else regions_for(duration_ms)
    for idx, (lo, hi) in enumerate(regions):
        if sum(1 for a, _, _ in cues if lo <= a < hi) < MIN_CUES_PER_HALF:
            result["why"] = "too few cues"
            return result
        lo_i, hi_i = int(lo // RES_MS), int(hi // RES_MS)
        c0, c1 = max(0, lo_i - margin), min(len(voice), hi_i + margin)      # the half plus the search margin: bounded memory
        v = voice[c0:c1].copy()
        v[:lo_i - c0] = 0.0
        v[hi_i - c0:] = 0.0
        lag, z = _best_lag(np, sub[c0:c1], v)
        if lag is None or z < MIN_Z:
            result["why"] = "no peak" if lag is None else "weak peak"
            result["halves"][idx], result["z"][idx] = lag, z
            return result
        result["halves"][idx], result["z"][idx] = lag, z
    h1, h2 = result["halves"]
    if abs(h1 - h2) > AGREE_MS:
        result["why"] = "halves disagree"
        return result
    result["offset_ms"] = int(round((h1 + h2) / 2))
    return result
