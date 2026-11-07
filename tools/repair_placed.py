#!/usr/bin/env python3
"""Subplot: send mis-timed placements back through the gate (run by hand as the service user; nothing on the media drives is touched here).
Input: the audit CSV lines (key,lang,source,offset_ms,h1,h2,z1,z2,why,secs) written by the read-only audit of every placed
sidecar with vad.py. For each placed file that the new measurement found
  - off by more than --limit ms with both halves agreeing: the sidecar's cues go back to incoming/ as a found pair (step
    "find", the mark's source and confidence); <prefix>-sync measures it again, shifts it (<= find.auto_shift_max_ms) and queues
    it, and <prefix>-place replaces OUR file (marker + manifest must match) and asks Plex to rescan;
  - "halves disagree" (another cut or a stretched subtitle) and --discard given: a "delete" decision is written; <prefix>-place
    removes our sidecar (only when the marker and the manifest match), clears the mark and the item goes back to Whisper.
Nothing happens without --apply; the default is a dry run that prints what would be done. Usage:
  python3 repair_placed.py audit-*.csv [--limit 1500] [--discard] [--apply]"""
import csv, glob, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C


def main(argv):
    files, limit, discard, apply = [], 1500, False, False
    it = iter(argv)
    for a in it:
        if a == "--limit":
            limit = int(next(it))
        elif a == "--discard":
            discard = True
        elif a == "--apply":
            apply = True
        else:
            files.extend(glob.glob(os.path.expanduser(a)))
    rows = [r for f in files for r in csv.reader(open(f, newline="")) if len(r) >= 9]
    shifted = discarded = skipped = 0
    for key, lang, source, off, h1, h2, z1, z2, why, *_ in rows:
        if not C.ITEM_KEY.fullmatch(key) or not C.LANG.fullmatch(lang):
            continue
        mark = C.marks(key)
        rec = (mark.get("placed") or {}).get(lang) if isinstance(mark.get("placed"), dict) else None
        if not isinstance(rec, dict) or rec.get("pending") or not isinstance(rec.get("path"), str):
            continue
        srt_i = os.path.join(C.INCOMING, f"{key}.{lang}.srt")
        js_i = os.path.join(C.INCOMING, f"{key}.{lang}.json")
        if why == "" and off not in ("", None) and abs(int(float(off))) > limit:
            if os.path.lexists(srt_i) or os.path.lexists(js_i):
                skipped += 1
                continue
            raw = C.read_bytes(rec["path"], C.MAX_SUB_BYTES)
            if raw is None or C.our_marker(raw) is None:
                skipped += 1                                              # not ours any more: leave it
                continue
            print(f"re-gate  {key} {lang} {source} off {int(float(off))} ms")
            if apply:
                try:
                    cues = C.parse_srt(C.strip_marker(raw))
                    data = C.write_srt(cues, (key, str(rec.get("source") or source or "find"), float(rec.get("confidence") or 0.7)))
                except (C.SubtitleError, ValueError) as exc:
                    print(f"  skipped: our placed file does not pass today's writer ({exc}); left as is")
                    skipped += 1
                    continue
                C.write_bytes(srt_i, data.encode() if isinstance(data, str) else data)
                C.write_json(js_i, {"key": key, "lang": lang, "step": "find", "source": rec.get("source") or source,
                                    "confidence": float(rec.get("confidence") or 0.7), "at": C.now(), "regated": True})
            shifted += 1
        elif why == "halves disagree" and discard:
            print(f"discard  {key} {lang} {source} halves {h1} / {h2}")
            if apply:
                C.write_json(os.path.join(C.DECISIONS, f"{key}.json"), {"decision": "delete", "at": C.now(), "by": "repair_placed", "why": "halves disagree"})
            discarded += 1
    print(f"{'applied' if apply else 'dry run'}: {shifted} re-gated, {discarded} discarded, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
