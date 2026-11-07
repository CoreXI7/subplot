#!/usr/bin/env python3
"""Subplot worker for the GPU PC (runs inside <PC_ROOT>\\venv). Usage: python subs_worker.py <job folder>
Reads job.json, audio.opus and frames/*.jpg from the job folder, writes out.srt and result.json there, exits 0.
It reads nothing outside the job folder, uses no network at run time and holds no credential (ARCHITECTURE.md "PC side").
result.json = {"confidence": 0..1, "no_speech_ratio": 0..1, "language_detected": "en", "burned_in": {"frames_with_text": n, "of": 12}}
confidence = duration-weighted mean of exp(avg_logprob) over segments, clamped to 0..1."""
import json, math, os, sys, time


def _add_cuda_dlls():
    """Windows: ctranslate2 needs cublas64_12.dll and cudnn*.dll; the nvidia-*-cu12 wheels ship them under site-packages/nvidia."""
    if os.name != "nt":
        return
    import glob
    for pattern in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_runtime/bin", "nvidia/cuda_nvrtc/bin"):
        for d in glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", *pattern.split("/"))):
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dlls()

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
EAST = os.path.join(MODEL_DIR, "frozen_east_text_detection.pb")


def fmt(ms):
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap_line(text, max_chars=42):
    lines = []
    cur = ""
    for word in text.split():
        while len(word) > max_chars:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= max_chars:
            cur = cur + " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


class _Word:
    """Small stand-in for a faster-whisper word when a segment carries none."""

    def __init__(self, start, end, word):
        self.start = start
        self.end = end
        self.word = word


def build_cues(words, max_cue_ms=7000, max_chars=42, max_lines=2, max_gap_ms=1500):
    """Turn faster-whisper words into cues: close at a sentence end once the cue holds 1 s, on a gap above
    max_gap_ms, when the cue would pass max_cue_ms, or when another word would pass max_lines * max_chars chars."""
    groups = []
    cur = []
    for word in words:
        text = str(getattr(word, "word", "") or "").strip()
        if not text:
            continue
        start = float(getattr(word, "start", 0.0) or 0.0)
        end = float(getattr(word, "end", 0.0) or 0.0)
        if cur:
            chars = sum(len(t) for _, _, t in cur) + len(cur) + len(text)
            if (start - cur[-1][1]) * 1000 > max_gap_ms \
                    or (end - cur[0][0]) * 1000 > max_cue_ms \
                    or chars > max_lines * max_chars:
                groups.append(cur)
                cur = []
        cur.append((start, end, text))
        if text[-1] in ".!?" and end - cur[0][0] >= 1.0:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    cues = []
    for index, group in enumerate(groups):
        start_ms = int(round(group[0][0] * 1000))
        end_ms = min(int(round(group[-1][1] * 1000)), start_ms + max_cue_ms)
        if end_ms < start_ms + 200:
            end_ms = start_ms + 200
        if index + 1 < len(groups):
            next_ms = int(round(groups[index + 1][0][0] * 1000))
            if next_ms > start_ms:
                end_ms = min(end_ms, next_ms)
        text = " ".join(t for _, _, t in group)
        lines = _wrap_line(text, max_chars)
        pieces = ["\n".join(lines[at:at + max_lines]) for at in range(0, len(lines), max_lines)]
        # several pieces share the group's span in order, each getting time in proportion to its characters (never overlapping)
        total_chars = sum(len(p.replace("\n", "")) for p in pieces) or 1
        at_ms = start_ms
        for i, piece in enumerate(pieces):
            share = (end_ms - start_ms) * len(piece.replace("\n", "")) // total_chars
            piece_end = end_ms if i == len(pieces) - 1 else max(at_ms + 1, at_ms + share)
            cues.append((at_ms, max(at_ms + 1, piece_end), piece))
            at_ms = piece_end
    return cues


def load_audio(path, rate=16000):
    """Decode the job's audio to mono float32 at 16 kHz with PyAV directly. faster-whisper 1.2's own decode_audio passes an
    argument PyAV 15+ no longer accepts (found 2026-11-07), and an array also sidesteps any future change there."""
    import av
    import numpy as np
    chunks = []
    with av.open(path, mode="r") as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        try:
            for out in resampler.resample(None):            # flush
                chunks.append(out.to_ndarray().reshape(-1))
        except (TypeError, ValueError):
            pass
    if not chunks:
        return np.zeros(0, dtype="float32")
    return np.concatenate(chunks).astype("float32", copy=False)


def transcribe(audio_path, task, lang, model_name):
    """faster-whisper large-v3 on CUDA (fp16, beam 5, VAD filter, word timestamps on). task 'transcribe' keeps the audio
    language; 'translate' produces English (Whisper only translates INTO English; a Spanish target is handled by the
    orchestrator as a second pass: see ARCHITECTURE 'gen.py' step 6, so for lang != 'en' this function must raise NotImplementedError
    until that pass exists). Returns (cues [(start_ms, end_ms, text)], confidence, no_speech_ratio, language_detected).
    Cues come from build_cues over each kept segment's words; skip segments whose no_speech_prob > 0.6; confidence =
    sum(exp(avg_logprob) * duration) / sum(duration), clamped 0..1; no_speech_ratio = duration of skipped segments /
    total audio duration."""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cuda", compute_type="float16", download_root=MODEL_DIR)
    audio = load_audio(audio_path)
    segments, info = model.transcribe(
        audio,
        task=task if lang == "en" else "transcribe",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    if lang != "en":
        raise NotImplementedError("non-English targets come in a later pass")

    job_dir = os.path.dirname(audio_path)
    last_progress = None

    def write_progress(done_ms):
        try:
            tmp_path = os.path.join(job_dir, "progress.json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"done_ms": int(done_ms)}, f)
            os.replace(tmp_path, os.path.join(job_dir, "progress.json"))
        except OSError:
            pass

    cues = []
    skipped = 0.0
    weighted = 0.0
    total_weight = 0.0
    for segment in segments:
        start = float(segment.start or 0.0)
        end = float(segment.end or 0.0)
        duration = max(0.0, end - start)
        now = time.monotonic()
        if last_progress is None or now - last_progress >= 5.0:
            last_progress = now
            write_progress(int(end * 1000))
        if float(segment.no_speech_prob or 0.0) > 0.6:
            skipped += duration
            continue
        weight = max(0.1, end - start)
        weighted += math.exp(float(segment.avg_logprob or 0.0)) * weight
        total_weight += weight
        words = getattr(segment, "words", None)
        if not words:
            words = [_Word(start, end, segment.text or "")]
        cues.extend(build_cues(words))

    confidence = weighted / total_weight if total_weight > 0 else 0.0
    confidence = max(0.0, min(1.0, confidence)) if math.isfinite(confidence) else 0.0
    total_duration = float(getattr(info, "duration", 0.0) or 0.0)
    total_ms = int(total_duration * 1000)
    if total_ms > 0:
        write_progress(total_ms)
    no_speech_ratio = skipped / max(total_duration, 1e-6)
    return cues, confidence, no_speech_ratio, info.language


def burned_in(frames_dir):
    """Count frames (of the 12) that contain text, with OpenCV's EAST detector (frozen_east_text_detection.pb in MODEL_DIR):
    a frame counts when at least 2 boxes with score >= 0.5 and width >= 40 px are found. Returns (frames_with_text, of)."""
    try:
        import cv2
        import numpy as np

        net = cv2.dnn.readNet(EAST)
        try:
            names = sorted(name for name in os.listdir(frames_dir) if name.lower().endswith(".jpg"))[:12]
        except OSError:
            names = []
        examined = len(names)
        count = 0
        for name in names:
            img = cv2.imread(os.path.join(frames_dir, name))
            if img is None:
                continue
            height, width = img.shape[:2]
            if abs(width / max(height, 1) - 4.0) <= 0.5:
                w, h = 640, 160
            else:
                w, h = 320, 320
            resized = cv2.resize(img, (w, h))
            blob = cv2.dnn.blobFromImage(resized, 1.0, (w, h),
                                         (123.68, 116.78, 103.94), swapRB=True, crop=False)
            net.setInput(blob)
            scores, geometry = net.forward(["feature_fusion/Conv_7/Sigmoid", "feature_fusion/concat_3"])
            boxes = 0
            rows, cols = scores.shape[2], scores.shape[3]
            for y in range(rows):
                score_row = scores[0, 0, y]
                geo_row = geometry[0, :, y]
                for x in range(cols):
                    if score_row[x] < 0.5:
                        continue
                    offset_x = x * 4.0
                    angle = geo_row[4, x]
                    cos = np.cos(angle)
                    sin = np.sin(angle)
                    right, bottom = geo_row[1, x], geo_row[2, x]
                    left = geo_row[3, x]
                    end_x = offset_x + cos * right + sin * bottom
                    start_x = end_x - (right + left)
                    if end_x - start_x >= 40:
                        boxes += 1
                        if boxes >= 2:
                            break
                if boxes >= 2:
                    break
            if boxes >= 2:
                count += 1
        return count, examined if examined else 12
    except Exception as exc:
        print(f"burned_in: {exc}", file=sys.stderr)
        raise                                                   # no result.json: the job fails instead of reporting "no text"


def main(argv):
    if len(argv) != 2 or not os.path.isdir(argv[1]):
        print("usage: subs_worker.py <job folder>", file=sys.stderr)
        return 2
    job_dir = os.path.abspath(argv[1])
    with open(os.path.join(job_dir, "job.json"), encoding="utf-8") as f:
        job = json.load(f)
    lang = str(job.get("lang", "en"))[:8]
    task = "transcribe" if job.get("task") != "translate" else "translate"
    model_name = str(job.get("model", "large-v3"))[:40]
    cues, confidence, no_speech, detected = transcribe(os.path.join(job_dir, "audio.opus"), task, lang, model_name)
    with open(os.path.join(job_dir, "out.srt"), "w", encoding="utf-8", newline="\n") as f:
        for i, (a, b, t) in enumerate(cues, 1):
            f.write(f"{i}\n{fmt(a)} --> {fmt(b)}\n{t}\n\n")
    n, of = burned_in(os.path.join(job_dir, "frames"))
    result = {"confidence": max(0.0, min(1.0, float(confidence) if math.isfinite(confidence) else 0.0)),
              "no_speech_ratio": max(0.0, min(1.0, float(no_speech) if math.isfinite(no_speech) else 0.0)),
              "language_detected": str(detected)[:8], "burned_in": {"frames_with_text": int(n), "of": int(of)}}
    with open(os.path.join(job_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
