# Architecture

How Subplot works, service by service. [README.md](README.md) covers installing it; [SECURITY.md](SECURITY.md) the
threat model in short.

## Shape

Subplot keeps a subtitle file next to every video in a Plex library. Six services share the code folder, a config folder
(`rules.json`, `.env`, optional `nav.json`) and one state folder. Unit names start with the install's `UNIT_PREFIX`
(`subplot` by default).

- `<prefix>-scan` - timer `<prefix>-scan`, 05:15 daily. Reads the nightly Plex database copy and the rules, writes `library.json` and `coverage.json`. No network, no secrets, no media access.
- `<prefix>-find` - starts on success of `<prefix>-scan`. Asks OpenSubtitles for the gaps, within budget and page requests first, and downloads to `incoming/`. Public network and `.env` only; no media access.
- `<prefix>-sync` - timer every 5 minutes, plus on success of `<prefix>-find`. Validates every file in `incoming/`, found or generated, converts it, measures speech against the video's audio, and routes it to `queue/` or `review/`. No network, no secrets; media read-only.
- `<prefix>-place` - timer `<prefix>-place`, every 5 minutes, plus on success of `<prefix>-sync`. Moves `queue/` files beside their videos, re-checks the decisions first, asks Plex to refresh the item, and acts on page decisions that touch files. The only media writer; loopback network only.
- `<prefix>-gen` - timer `<prefix>-gen`, every 10 minutes. When the GPU PC is idle, sends one item's audio and 12 frames to that PC over SSH, runs the worker there, and brings the SRT and `result.json` back to `incoming/`, where the sync timer picks them up. Audio and frames are extracted on the server. Network to the GPU PC's SSH only.
- `<prefix>-board` - long-running server (`server.py`) on a unix socket that only the reverse proxy's group can open; the proxy signs the owner in. Reads state; writes only `decisions/` and `requests/`. No network, no secrets, no media.
- `<prefix>-place.path` - starts `<prefix>-place` as soon as the board saves a decision (it touches `decisions/.kick`).

A file flows one way. A service writes `incoming/<key>.<lang>.srt` and a sidecar `incoming/<key>.<lang>.json`. `<prefix>-sync` validates and measures it, then moves it to `queue/` or `review/`. `<prefix>-place` moves a queued file beside its video and asks Plex to refresh. A file in `review/` waits for the owner's decision on the board. Every SRT written carries the marker line `; <MARKER_TAG> <key> <source> <confidence>` as its last line.

The repository holds the services in `subplot/` (`common.py`, `scan.py`, `find.py`, `sync.py`, `vad.py`, `gen.py`, `place.py`, `server.py`, `static/` for the page, `models/` for the speech detector), the unit templates in `units/`, `install.sh` with `tools/install.py`, the example settings in `config/`, the GPU PC's worker in `pc/`, `tools/repair_placed.py` and `tools/pre-push`, and the tests.

State lives in one folder, with the paths as constants in `common.py`:

| Path | Meaning |
| --- | --- |
| `library.json` | Every item: key, Plex ids, paths, duration, audio, subtitle streams, views, need. `scan.py` is the only writer. |
| `coverage.json` | Counts per library from the last scan, plus a stale flag. |
| `plex.db` | The copied nightly Plex database, replaced on each scan. |
| `rules.last-good.json` | The last `rules.json` that validated. Any service may refresh it. |
| `incoming/<key>.<lang>.srt` + `.json` | A file before the gate. `find.py` and `gen.py` write; `sync.py` consumes. |
| `queue/<key>.<lang>.srt` + `.json` | Passed the gate, waiting for `<prefix>-place`. Written by `sync.py`. |
| `review/<key>.<lang>.srt` + `.json` | Needs a look. Written by `sync.py`; removed by `place.py` on a decision. |
| `decisions/<key>.json` | The owner's decision. Written by the board, read and acted on by `place.py`. |
| `marks/<key>.json` | Service marks: `asked_at`, `burned_in`, `gen_failed`, `placed` with path, inode, hash and time. |
| `requests/<id>.json` | Page requests: id, kind, key or show, note, time. The board writes; find and gen unlink on pickup. |
| `budget.json` | Day, downloads, searches, `exhausted_until`. |
| `tokens/os.json` | The OpenSubtitles token, mode 0600; `find.py` only. |
| `vad-ok` | Exists while the speech detector loads; the sync service refreshes it every run. |
| `current.json` | The item a service works on now. |
| `pc.json` | Last GPU PC probe: time, idle, reason, idle since, last run, GPU use, VRAM state, unload state. |
| `gen_hours.json` | Generation seconds used this night; the night starts at 18:00 local. |
| `es_kept.json` | How many Spanish files the owner kept from review. |
| `pc/<key>/` | Per-job transfer folders, deleted after each job. |
| `done.log` | One JSON line per outcome. |
| `actions.log` | One line per scan or place action. |

All code is Python 3.12 standard library, plus `ffmpeg` and `ffprobe` as fixed external programs with argument lists and timeouts; the sync service also uses numpy and onnxruntime from its own venv. Paths and settings come from `SUBS_*` environment variables that `install.sh` writes into the units.

## rules.json

The owner edits this file on the server. Only `path_map` and `media_roots` are required; every other key falls back to
`common.DEFAULTS` (the right-hand column). It is validated on load; the last valid copy stays in effect. For the keys the
page can edit (next section) this file's value is the starting point, and for the quality and timing limits it is the
floor the page can't go below.

| Key | Meaning | Default |
| --- | --- | --- |
| `path_map` | Plex container path to host path. Keys absolute; each value under a media root. | One entry per Plex mount |
| `media_roots` | Host folders that subtitles may be written under. | The media drives |
| `exclude_libraries` | Plex libraries that are never scanned. | `["Hentai"]` |
| `plex.machine_id` | 32 to 40 hex digits identifying the Plex server (for Open in Plex links). | Required |
| `languages.primary` | The language that is always required. | `en` |
| `languages.secondary` | Second language, added when a listed user watched the item. | `es-419` |
| `languages.secondary_for_users` | Plex usernames whose viewing triggers the secondary language. | Empty list |
| `thresholds.keep` | Whisper confidence at or above which a generated file is placed without review. | `0.85` |
| `thresholds.review` | Below keep but at or above this, a generated file waits in Needs a look; below it, it is dropped. | `0.60` |
| `find.daily_budget` | Downloads per UTC day. | `1000` |
| `find.min_score` | Lowest accepted OpenSubtitles score, 0 to 100. | `25` |
| `find.sync_max_offset_ms` | Largest subtitle offset the sync check accepts, in ms. | `1500` |
| `find.auto_shift_max_ms` | Largest offset the sync step corrects on its own, in ms. | `60000` |
| `find.retry_days` | An item is not asked for again within this many days. | `30` |
| `generate.model` | Speech-recognition model on the GPU PC. | `large-v3` |
| `generate.max_hours_per_night` | Generation budget per night. | `6` |
| `generate.burned_in_frames` | Frames sampled per item to detect burned-in subtitles. | `12` |
| `generate.burned_in_ratio` | Share of sampled frames with burned-in text that marks the item. | `0.5` |
| `generate.es_review_until_kept` | Spanish output stays in review until the owner has kept this many. | `20` |
| `pc_idle.hours` | Windows when the GPU PC counts as idle. | `["mon-fri 00:00-17:00", "mon-fri 23:30-23:59", "sat-sun 02:00-09:00"]` |
| `pc_idle.no_processes` | Programs that make the PC busy (games in a game library are found on their own). | `["VRChat.exe"]` |
| `pc_idle.not_games` | Programs inside a game library that do not count as games (for example a wallpaper app). | a list of names |
| `pc_idle.gpu_util_below` | GPU use below this percent counts as idle. | `20` |
| `pc_idle.vram_free_mb` | Free VRAM needed before a job, in MB. | `6000` |
| `pc_idle.yield_to` | Programs the worker waits for. | `["ollama.exe"]` |
| `pc_idle.unload_idle_model.ollama` | The PC's Ollama, whose idle model may be unloaded inside the idle hours. Optional. | `http://10.0.0.50:11434` |
| `pc_idle.unload_idle_model.after_min` | Idle minutes before the model is unloaded. | `10` |
| `priority.watched_days` | Window for view counts. | `30` |
| `priority.libraries` | Library order for picking work. | `["TV Shows", "Anime", "Movies"]` |

Validation checks: `path_map` keys absolute, values existing directories under a media root; `machine_id` 32 to 40 hex digits; language tags `[a-z]{2,3}(-[A-Za-z0-9]{2,4})?`; usernames 1 to 60 characters of `[A-Za-z0-9_.@-]`; thresholds in 0 to 1 with review not above keep; budgets integers 1 to 1000; hour windows as a day range and `HH:MM-HH:MM`; process names 1 to 60 characters of `[A-Za-z0-9_.-]`; ratios 0 to 1. A bad file is logged with a reason code and `rules.last-good.json` is used instead. A service with no valid rules at all exits 0 and does nothing.

## Settings from the page

The board writes `settings/settings.json` (`{"version", "at", "by", "values"}`, values nested like rules.json; only the
keys in `common.PAGE_KEYS`) atomically under a lock, with a version check (409 when someone saved in between) and one
history line per save (key names, never values). Every service reads it through `load_rules()`: file rules, then
`DEFAULTS` for missing keys, then the page values merged on top. Reading fails safe: an unknown key or a malformed file
ignores the page values (except exclusions, which only take things away); a value out of range, or a stricter-only value
looser than the file (`thresholds.keep`/`review`, `find.min_score` may only rise; `find.sync_max_offset_ms`,
`find.auto_shift_max_ms` may only fall), is dropped and the file's value stays. Exclusions are a union: the page can add
libraries, never remove the file's or the code's. The page never sees or edits `path_map`, `media_roots`, the Ollama
address, secrets or install-time settings. API: `GET /api/settings`, `POST /api/settings {"version","values"}`.

## common.py

Shared constants: every state path above, `RULES`, `ENV_FILE`, `PLEX_DB_NIGHTLY` (`SUBS_PLEX_DB`), `PLEX_URL` (`SUBS_PLEX_URL`, loopback port 32400), `PC` (`SUBS_PC`, the GPU PC's SSH name), `PC_ROOT` (`SUBS_PC_ROOT`, the worker folder on the GPU PC, given with forward slashes), `MARKER_TAG` (`SUBS_MARKER_TAG`), `FFMPEG`, `FFPROBE`, `EXCLUDE_LIBRARIES` (`Hentai`, which a rules file cannot remove) and `SECRET_RES`.

Regexes: `PLEX_ID`, `LANG`, `ITEM_KEY` (`ep-<id>` or `mv-<id>`), `DECISION` (`keep`, `shift`, `regenerate`, `delete`, `exclude`), `REQUEST_KIND` (`find`, `generate`), `REQUEST_ID`, `SRT_TIME` (a comma or a dot is read; a comma is always written) and `MARKER`.

It provides rules loading and redacting stderr logging; capped JSON reads and atomic 0600 JSON writes; log appends and the `done()` outcome line; regular-file opens with `O_NOFOLLOW` and `O_NONBLOCK` and lstat listings; `safe_media_path()`, which walks a host path through a realpath and lstat chain under a media root and rejects anything but a regular file with a safe name, and `sidecar_path()`.

For subtitles it provides the marker reader and writer (`our_marker`, `write_srt`), `parse_srt`, `validate_subtitle` with the file limits, and `ass_to_srt`. For work it provides `item_key`, the UTC-day budget guard `budget_take`, `budget_exhaust`, marks and decisions (`mark`, `marks`, `decision`), the GPU PC check (`pc_idle`, `gpu_busy_others`), `night_id`, `requests_pending`, `excluded_library`, `new_request_id`, plus small file and path helpers.

## scan.py

Runs as `<prefix>-scan` daily at 05:15. No network, no secrets, no media access.

1. Copy `PLEX_DB_NIGHTLY` to `PLEX_DB_COPY`. A copy older than 26 hours or missing sets `coverage.json.stale = true`, keeps the other fields, and ends the run, so a missed database copy is visible the same morning.
2. Query episodes and movies separately over `metadata_items` joined to `library_sections`, with the show and season parents for episodes. Excluded libraries are filtered in that SQL, so an excluded item never enters a row. Helper queries (media parts and streams, views, taggings as a fallback for ids) are joined in Python to the selected items only.
3. Each row holds key, Plex ids, kind, library, section, show and season data, parsed external ids (tvdb, imdb, tmdb, anidb), the Plex path, the host path or null, status `ok` or `unmapped`, duration, audio streams, subtitle streams, view counts, added time and the needed languages. The path mapping is lexical, because the scan has no media access; an unmapped item gets no need and its path is never probed. Services that open the file run `safe_media_path` first.
4. The need list is `["en"]`, `["en", "es-419"]`, `["es-419"]` or empty. The secondary language is added only when a listed user watched the show, or the movie itself, inside the priority window. A language counts as present when any text, image or sidecar stream has it.
5. Write `library.json` and `coverage.json`. Coverage gives per library the item count, English count, Spanish count, items with no subtitle, image-only items and generatable items, plus a total. Over 100,000 rows nothing is written and coverage is marked stale. A `burned_in` mark removes an item from generatable while its status stays `ok`.

## find.py

Runs as `<prefix>-find`, on success of the scan. Reads `.env`, uses the public network only, never touches media, and is capped at 60 minutes.

**Pacing.** The API throttles at about 5 requests per second with HTTP 429; that is not the daily quota, which is 406 or `remaining == 0` on a download. The client keeps at least 0.25 s between calls, retries one 429 after `Retry-After` (1 to 15 s, default 3), and ends the run on a second 429 with the budget untouched.

**Requests.** The API answers 301 to any `/subtitles` URL that is not canonical and the client does not follow API redirects, so the query must be built canonically: parameters sorted alphabetically, no parameter at its default value (`machine_translated=exclude` is the default and is never sent), imdb ids as plain numbers. Plex gives each episode its own imdb and tmdb id, and OpenSubtitles lists episodes as features, so every item, movie or episode, is searched by `imdb_id` else `tmdb_id`. No season, episode, parent or tvdb parameters are sent. A test asserts the built URL.

**Order.** Page requests of kind `find` come first: a show request expands to that show's items with a need, and a movie whose id matches too. The request file is unlinked on pickup and its id logged. Then items with a need, ordered by views, library priority and added time, newest first.

**Skips.** An item is skipped when its status is not `ok`, when it is decided `exclude`, when `asked_at` is within `retry_days`, when a file for that key and language is already in `incoming/`, `queue/` or `review/`, or when it is marked `burned_in`.

**Budget.** The run stops at `daily_budget` downloads or five times that many searches. The day is UTC. When the daily quota is exhausted, `exhausted_until` is set to now plus 24 hours, or to the reset time the API reports, and the run ends. A malformed `exhausted_until`, or one more than 48 hours ahead, is dropped so a bad value cannot wedge the budget.

**Login.** One `POST /login` per 23 hours, a margin under the 24-hour token life. The token is written to `tokens/os.json` with mode 0600.

**Scoring.** The score is 0 to 100:

| Part | Points |
| --- | --- |
| Trusted uploader | 40 |
| Movie hash match against the video | 30 |
| Download count | Up to 30, 10 per decade |
| Site rating | Up to 10 |

The rating is 0 for most files, so it never gates alone. The pick must reach `min_score`; then a trusted uploader wins, then a movie hash match (an exact release beats popularity), then download count, then score.

**Download.** `POST /download` with the `file_id` returns a link that must be on `*.opensubtitles.com`. The file is fetched with a 2 MiB cap; one redirect is followed when it stays on that domain. Errors are stored as reason codes.

**Stored.** `validate_subtitle` runs on the downloaded bytes. It drops zero-length cues and refuses a file labelled `en` or `es` whose text is plainly another language (under 80 % Latin letters, or the other language's common words winning 1.5 to 1 over at least 20 known words); a cue the writer still refuses fails that file only, never the run. A refusal is logged as `rejected` and the item's `asked_at` is set. An accepted file is written to `incoming/<key>.<lang>.srt` with the marker as its last line, `source = os:<file_id>` and confidence = score / 100, plus `incoming/<key>.<lang>.json` with key, language, source, confidence, score, the `hi` flag, time and step. `asked_at` is set either way. Found files are gated by `min_score` and the sync check, not by `thresholds.keep`, which applies to generated text.

## sync.py

`<prefix>-sync` turns incoming subtitle pairs into queue or review pairs. It has no network access, holds no secret
and opens media read-only. For each `incoming/<key>.<lang>.srt` plus `.json`:

- The json must carry `step` (`find` or `gen`) and a numeric `confidence`; otherwise the pair is rejected with
  `why: "metadata missing"`. An invalid subtitle is rejected too: `done(result="rejected")` and the pair is deleted.
- The subtitle is re-validated, then the video is located through `library.json` and `safe_media_path`. A missing video,
  or one with `status != ok`, goes to review with `why: "video missing"`.
- The subtitle is measured against the speech in the video and routed.
- The file is rewritten from the validated cues (sanitised text, marker as the last line). The json gains `offset_ms`
  (the shift that was applied, null when none, so an in-sync file shows no Fix-timing button), `measured` (the verdict on
  the file as it arrived: `offset_ms`, `halves`, `z`, `why`, plus `after_shift` when a shift was tried) and `why`.
- A speech failure goes to review with one of `ffmpeg exit <rc>`, `ffmpeg: <reason>` (ffmpeg cannot start),
  `ffmpeg did not exit`, `audio decode timed out`, `audio too long`, `audio too short`, `no audio`.

Routing reads `language`, `lang` or `language_detected`, the pair's own language (from the file name) first. Defaults
when the rules omit them: `sync_max_offset_ms` 500, `thresholds.keep` 0.85, `thresholds.review` 0.6.

| input | route |
| --- | --- |
| `step == "find"` | queue |
| `step == "gen"`, confidence ≥ keep | queue |
| `step == "gen"`, confidence ≥ review | review, `why`: confidence below keep |
| `step == "gen"` below review | discarded, `mark(gen_failed += 1)` |
| generated `es` or `es-419` | review until `es_kept.json.kept ≥ es_review_until_kept` |

A badly named incoming file is logged and skipped, with no `done` line. Every review json carries `preview` (the first 40
cues) when there are cues. Every file gets its `done()` line, including one whose processing crashed
(`result: "failed"`); sync lines carry `measured_ms`.

Timing policy (`apply_timing`) applies to found files only:

| measurement | result |
| --- | --- |
| no agreed offset | timing verdict `timing not confirmed (<why>)`; the item stays missing |
| offset within `find.sync_max_offset_ms` | the original cues are kept and queued |
| offset within `find.auto_shift_max_ms` (60 s) | the original cues are shifted, then measured again: within the limit they are queued; otherwise `off by N s; shift did not fit (...)` |
| offset beyond `auto_shift_max_ms` | `off by N s`; discarded |

`shift_cues` drops a cue that ends at or before 0, clamps a start before 0 to 0 and never produces a zero-length cue.
Timing verdicts never go to review: the item stays missing and gen produces a fitting file. Only non-timing failures
(ffmpeg, a missing video) reach Needs a look. A file the owner shifted (`shifted_by`) that still misses is explained in
review. A generated file is never shifted: Whisper transcribes the audio itself, so a measured offset says something
about the measurement, not the file. Its `measured` is logged as free ground truth and `offset_ms` stays null.

`repair_placed.py` (run by hand) sends a placed sidecar that an audit found off back through the gate as a found pair
with `regated: true`. When that pair is discarded, sync writes a `delete` decision (`by: "sync"`, under the per-key lock,
never over an owner decision), so place removes the stale sidecar and clears the mark; when it is queued, place replaces
our own file.

`vad.py` measures speech. ffmpeg decodes the video's default audio track to 16 kHz mono PCM on a pipe (`-threads 2`,
900 s wall clock, at most 5 h of audio, at least 60 s). The Silero VAD model (`models/silero_vad.onnx`, MIT, sha256
pinned in `vad.MODEL_SHA256`) runs on the CPU through onnxruntime (2 threads) over every 512-sample window with 64
samples of context. Speech spans use threshold 0.5; gaps under 300 ms are merged, spans under 200 ms dropped, and spans
padded by 60 ms. A track up to 16 min (`WHOLE_FILE_MAX_MS`) is decoded whole and its two halves are the measured
regions; a longer one is decoded only inside two 6-min windows centred at 25 % and 75 % (`regions_for(duration_ms)`,
Plex's duration, `ffmpeg -ss/-t`), so the check reads about 12 min of any file instead of all of it. `measure(cues,
spans, duration_ms, regions)` turns cues and spans into 10 ms on/off signals and finds, for each region on its own, the
lag within ±120 s that maximises their cross-correlation (FFT). Each region must hold at least 10 cues, each peak must
stand at least 2.5 standard deviations above the median of the searched lags (`MIN_Z`), and the regions must agree within
1 s. Otherwise `offset_ms` is null with `why` `too few cues`, `halves disagree` or `no peak` (a flat signal, or a peak
on the search edge). Positive means the cues must move later; `z` is logged, not gated. Signals are cropped to each half
plus its ±120 s margin before the FFT, the lag range is capped at the signal's length, and a peak that sits on, or ties
with, a search edge is `no peak`. The detector loads on every run, before the empty-incoming return, so `STATE/vad-ok`
exists exactly while it loads (an install can check it); a missing module or model makes `load()` raise
`Unavailable`, and sync logs once and leaves incoming/ untouched. numpy and onnxruntime live in a dedicated virtualenv
which the unit binds read-only; the unit must allow `mbind` and `prlimit64` back from `@resources`, or the process dies
with SIGSYS.

`validate_subtitle` stays linear on hostile input: the ASS detection is anchored to a single line, and 2 MiB of newlines
is parsed in under 2 s. `write_srt` raises `ValueError` on a cue the parser would refuse (start < 0, end ≤ start,
≥ 100 h) and on a non-finite confidence.

## gen.py

`<prefix>-gen` runs every 10 minutes. Its only network is SSH to the GPU PC; it holds no secret and opens media
read-only. It probes the PC with `pc/probe.ps1` over SSH. The fixed script returns `gpu_util`, `gpu_mem_used_mb`,
`gpu_mem_total_mb`, `busy_procs` (names of processes at ≥ 10 % GPU sm per `nvidia-smi pmon`), `processes`, `game_procs`
(processes running from a game library) and `hour`; the result is written to `pc.json`, with `idle_since` kept from the
previous file while idle.

`pc_idle` requires all of:

- the clock is inside `pc_idle.hours` (entries `HH:MM-HH:MM`, optionally prefixed by a day or day range such as `mon`,
  `tue-fri`, `sat-sun`; `fri-mon` wraps; a span crossing midnight belongs to the day it starts on);
- no listed process is running (only game executables; the store client is not listed);
- no process is running from a game library (`game_procs`: any process whose path contains a game-library directory;
  `pc_idle.not_games` names the utilities that live there too; a probe without the field blocks);
- `gpu_util < gpu_util_below`;
- no other process is working the GPU;
- at least `vram_free_mb` is free (a model loaded but idle in VRAM is fine while it fits).

Not idle, or `gen_hours.json` for the current `night_id` ≥ `max_hours_per_night`, means exit 0. While the worker runs,
gen probes every 30 s. The moment any other process works the GPU (`yield_to` names first, then anything but our own
worker) it kills the worker on the PC (`run_worker_watched` plus `kill_remote_worker`), marks the item
`interrupted_at` (not a failure, no `gen_failed` increment), logs `result: "interrupted"` and exits. The item is picked
again on a later idle tick. The queue is never discarded and the owner's model never waits.

When `pc_idle.unload_idle_model` is set and free VRAM is the only failing check (`pc_idle` passes with
`gpu_mem_used_mb` forced to 0) while `ollama.exe` is among the processes, gen records `vram_blocked_since` in `pc.json`.
Once that is `after_min` minutes old (default 10, i.e. the second tick) it asks the PC's Ollama for its loaded models
(`GET /api/ps`, 64 KiB cap, names must match `[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}`, at most 8) and unloads each
(`POST /api/generate` with `model` and `keep_alive: 0`). It logs the unload, writes `unloaded` into `pc.json`, sleeps
5 s, probes again and continues only if `pc_idle` now passes. The `ollama` URL is validated as plain `http://` to a
private IPv4 and port (the unit already allows the PC's address). Ollama has no auth, so no secret is involved. The
owner's next prompt reloads the model and the yielding rule stops our worker.

The Generate now switch is stored in `decisions/pc-generate-now.json`. `common.pc_override()` is true while its `until`
is in the future and at most 24 h ahead. `pc_idle(..., override=True)` waives only the idle hours: listed processes, any
game-library process, GPU use by others and the VRAM check still block, and the watcher still yields. gen passes the
override to both `pc_idle` calls and to the Ollama-unload rule, so Generate now may unload an idle model. `pc.json`'s
`why` gets `(generate now)` appended while it applies.

The job round trip:

1. Pick one item: a `generate` request first (unlinked on pickup; a keyless request names a show or movie and takes its
   first eligible item), else the top generatable English item (no audio track tagged another language; untagged audio
   counts) by views, library priority and `added_at`. Skip `status != ok`, decided `exclude`, `burned_in`,
   `gen_failed ≥ 2`, items whose placed subtitle for the language still exists (unless a request asks), items with a pair for the language in incoming/queue/review, items with an image subtitle in the
   language, and `en` items without an English audio track.
2. On the server: ffmpeg makes `audio.opus` from the first English-tagged audio track, else the first track (mono, 16 kHz, 32 kb/s, 300 s timeout, ≤ 60 MiB) and 12 frames from the
   bottom 25 % of the picture at 5..95 % of the duration, 640 px wide, JPEG q4, ≤ 200 KiB each. Fewer than 6 usable
   frames fails the job (the burned-in check needs them). `job.json` carries `key`, `lang`, `task`
   (`transcribe` or `translate`), `audio_lang` and `duration_ms`; nothing else is sent.
3. `scp` the job folder, then run the worker over SSH (40 min timeout; the probe is the text of `pc/probe.ps1` sent as
   the SSH command, and the PC shell is pwsh). The worker writes `out.srt` and `result.json`. A remote size check
   (`out.srt` ≤ 2 MiB, `result.json` ≤ 4 KiB) runs before `scp` back, and the remote folder is deleted afterwards,
   also on failure.
4. On return, `result.json` is validated. A burned-in result (`frames_with_text / of ≥ burned_in_ratio`) becomes
   `mark(burned_in=True)` and `done(result="burned_in")`, with nothing kept. Otherwise `validate_subtitle` writes an
   incoming pair with a marker (`source = gen:<model>`, `confidence`) and json (`step: "gen"`), and the job's wall time
   is added to `gen_hours.json`. An invalid `result.json`, a worker non-zero exit, a timeout, a missing video, or a
   failed audio or frame extraction gives `mark(gen_failed += 1)` and `done(result="failed", why=code)`. gen only
   writes `incoming/`; the sync timer runs every 5 minutes and picks the pair up.

## place.py

`<prefix>-place` is the only writer of media. It runs every 5 minutes, after each `<prefix>-sync` run, and at once when the
board saves a decision (`<prefix>-place.path` watches `decisions/.kick`); a run loops over decisions and the queue up to
three times while new decisions arrive. It has media read-write access, talks to Plex over loopback
only, and reads a `.env` file for the Plex token and nothing else. Decisions come first: for each `decisions/<key>.json`
with no `acted_at`:

| decision | action |
| --- | --- |
| `keep` | move the review pair to queue |
| `regenerate` | remove the review pair, write a `generate` request with the note |
| `delete` | remove our own sidecar (marker plus manifest); the review pair goes only when every unlink succeeded, else `delete failed; try again`; drop `placed[lang]` from the mark (`placed` itself becomes null when empty) |
| `exclude` | remove incoming/queue/review files for the key; nothing is written to media |

`keep` copies by hard link and never clobbers an existing queue pair. The decision file then gains `acted_at` and
`result` (place may write `decisions/`; a decision dict passed directly, as the tests do, is recorded there with
`by: "direct"`). A decision already acted on, or whose `at` changed under us, returns `superseded`. Each key is handled
under `decisions/<key>.lock`, shared with the board.

For each `queue/<key>.<lang>.srt` plus `.json`: the decision must not be `exclude`, and the item must exist in
`library.json` with `status == "ok"` and a non-excluded library. The sidecar goes beside the item's current path; a
release swapped in between gets a new name and the next scan re-finds it. A queue pair without our marker is dropped
with `done(result="discarded")`. The paths come from `safe_media_path` and `sidecar_path`, and a foreign subtitle at
that name moves the pair to review with `why: "a subtitle we did not write is already there"`. The journal entry
`placed[lang] = {path, sha256, pending: true}` is written first; the file then goes through the pinned directory fd (a
temp file `O_EXCL` at 0644, `os.link` for a new name so nothing is ever clobbered, and a fd-verified `os.replace` only
over our own file). A change under us goes to review with `the subtitle changed while we were writing`. Then
`mark(placed[lang] = {path, inode, sha256, at, source, confidence})`, the queue pair is deleted, and
`done(result="placed")` is logged.

The marker is the last line of every file we write, because Plex's scanner ignores a sidecar whose first line is the
marker. Reads accept it as the last non-empty line, or as the first line of an older file; `strip_marker` removes it
from either end. `own_sidecar(path, key, lang)` confirms a file is ours from the marker plus the manifest. A delete
decision that removed a sidecar asks Plex for the same folder rescan (else Plex keeps listing the gone file). After a placement, place asks Plex for a partial scan of the video's folder (`GET /library/sections/<id>/refresh?path=`)
when the item carries its section and Plex path, else `PUT <PLEX_URL>/library/metadata/<plex_id>/refresh`, with
`X-Plex-Token` (5 s timeout; a failure is logged as a code and the file stays). `rotate_done_log` keeps done.log
under 5 MB by dropping its older half. A kept generated Spanish file (`step == "gen"`) increments `es_kept.json`.

## The board

`<prefix>-board` serves the page and API on a unix socket only (mode 0660, group = the reverse proxy's); it never
listens on TCP, has no network access and holds no secret. Signing in is the reverse proxy's job unless `AUTH=login`
(below). State is read-only except `decisions/` and `requests/` (and `auth/` with `AUTH=login`). The server is single-threaded with a cache keyed on mtimes. Request hygiene:

| check | response |
| --- | --- |
| Host missing | 400 |
| Host not `SUBS_HOST` | 421 |
| path with `%`, `..`, `//` or a backslash (as sent, before any rewrite) | 400 |
| method other than GET/POST | 405 |
| POST without `X-Subs: 1`, or with Origin / Sec-Fetch-Site present and not same-origin | 403 |
| body over 8 KiB | 413 |
| POST without Content-Length | 411 |
| GET with a body | 400 |

Responses carry security headers and `Cache-Control: no-store`. A browser always sends both cross-site headers, so their absence on a POST means the
caller is not a browser. Every POST can at most write one small JSON file under `requests/` or `decisions/`: a request
never touches media, never starts a job and never changes rules.

| POST | effect |
| --- | --- |
| `/api/request {"kind","key"\|"show","note"?}` | `requests/<id>.json`; 409 when a request is already pending for the same key/show or when 50 are pending; 404 for an unknown key/show |
| `/api/decision {"key","decision","note"?}` | `decisions/<key>.json`; `exclude` for any library item, `keep`/`shift`/`regenerate`/`delete` only for a key with a review file |
| `/api/pc {"generate_now","hours"?}` | writes or removes `decisions/pc-generate-now.json` |
| `/api/settings {"version","values"}` | `settings/settings.json` (see "Settings from the page"); 400 names the field, 409 on a stale version |

The read API is GET only. `GET /api/settings` returns the page values, the server file's values and the merged ones for
the page-editable keys only. `GET /api/state` returns `at`, `coverage` per library (`items`, `en`, `es`, `none`,
`image_only`, `generatable`, `burned_in`, `ours`, `review`), `stale`, `scan_at`, `idle_hours`, `tonight` (`found`,
`generated`, `placed`, `burned_in`, `rejected`, `failed`, `since`, counted from done.log since the current `night_id`),
`recent` (the last 50 done entries, without paths), `review` cards, `queue` count, `budget` (`downloads`, `limit`,
`exhausted_until`), `pc` (`at`, `idle`, `why`, `idle_since`, `last_run`, `gpu_util`, `generate_now_until`), and
`requests`. `GET /api/missing?q=` returns shows and movies matching a case-insensitive substring (≤ 60 chars, ≤ 50
results) with `missing_en`, `missing_es` and `burned_in`. `GET /api/show/<plex id>` returns episodes with status per
language: `have`, `queued`, `review`, `burned_in`, `missing` or `excluded`.

`burned_in`, `ours` and `review` are counted live from `marks/`, `review/` and `library.json`. `tonight` counts done.log
lines, so one item can count as found and placed; `generated` is gen's queued or review results. A burned-in item leaves
`missing_en` but still counts in `missing_es`. The board also serves the page files (`index.html`, `base.css`, `subs.css`, `subs.js`, `subs-views.js`, `favicon.svg`)
with its own CSP, and `GET /api/config` = `{"nav": [...]}`: up to 8 header tabs from `nav.json`, each label 1-24 plain
characters and each link a single-slash path or an `https://` URL. `SUBS_OVERLAY` may name a folder whose copies of those
same page files are served instead (opened once at start with `O_NOFOLLOW`; each file read through `openat` and
`O_NOFOLLOW`, regular files under 1 MiB only; the board refuses an overlay that is, holds or sits inside the state or
config folder). A review card's `plex_url` is the Plex web app link built from the server's machine id and
the item id. No payload ever contains a host path (a test greps for `/media/`).

## The board's own sign-in (AUTH=login)

`subplot/auth.py` (the account, sessions, throttle, TOTP and recovery codes; ported unchanged in substance from Fishbowl)
and `subplot/signin.py` (the request side, a mixin of the board's handler). Files live in `STATE_DIR/auth/` (0700, files
0600), bound into the board only in this mode and hidden from every other service. Every request still arrives through
the proxy on the socket: a request counts as HTTPS only with exactly one `X-Forwarded-Proto: https`, and sessions are made
and accepted only then; the single `X-Real-IP` is used to count tries and to show where a sign-in came from, never to
grant anything. Without a session the page redirects to `login` and the API answers 401 `{"why", "signin": true}`;
`/login`, `/login.js`, `/base.css`, `/favicon.svg`, `/api/auth/state` and the four sign-in POSTs are the only public
paths. Every account change needs the current password again (and a code when two-step is on). The account is created
with a one-time setup code from `auth.py setup-code`, run as the service account; the board never makes or logs one.
With `AUTH=proxy` none of this runs and the board answers exactly as before (test_board_modes.py).

## The page

Phone-first, one scroll, with the brand "subplot" and optional header tabs from `api/config`. All URLs are relative, so
the page works under any path prefix. All DOM is built through `el()`, text nodes and `setAttribute`; polling is every 30 s, and `Subs service unavailable` keeps the last data on screen.

1. **Coverage** — one bar per library (English filled, Spanish as a thin second bar) with have, missing, burned-in, ours
   and needs-a-look counts. A stale scan shows a yellow line saying the scan date and that the Plex database copy is old.
2. **Progress** — the Tonight line (found, generated, placed, burned-in, PC idle time and next window) and the Generate
   now switch. The switch is a checkbox with `role=switch` plus a hint; it POSTs `{"generate_now": true, "hours": 6}`
   (hours 1..12) and off sends `{"generate_now": false}`. The page mirrors `pc.generate_now_until` on each poll,
   disables the input while a POST is in flight, and shows "On until HH:MM".
3. **Needs a look** — a card per review item: show, `SxxEyy`, library, source and confidence, `why` when present, and the
   preview cues as scrollable text. The number is labelled "source score" for OpenSubtitles files and "confidence" for
   generated ones. Buttons: Keep (filled) / Regenerate… / Delete / Open in Plex. The card leaves when place has acted,
   on the next poll. A card whose json carries a numeric `offset_ms` replaces Keep with its filled
   **Fix timing · N s earlier|later** button (Keep becomes "Keep as is"). `POST /api/decision {"decision": "shift"}` is
   accepted only for such a review pair (409 otherwise). place applies the offset to the original cues (clamped at 0,
   cues that would end before 0 dropped), writes the file with the same marker into incoming/ with `shifted_by`
   (accumulated) and no `why`, `preview` or `offset_ms`, drops the review pair and logs `result: "shifted"`. sync then
   runs the whole gate again: in sync → queue → placed; still off → review again with
   `off by N s (after a X s fix; a single shift does not fit this file)`.
4. **Missing** — a search box; rows such as "Show · 610 missing English · 12 burned-in" with Find and Generate for the
   whole show (disabled with "queued" while a request is pending); a row expands to its episodes with per-episode
   status and the same two buttons.
5. **Recent** — the done.log tail as rows (time, step, show, result, why), folded by default.

Look: `base.css` holds the colour and type tokens and base elements; `subs.css` styles the blocks. One screen tells the whole
story on a phone without scrolling; coverage bars animate once on load; figures are tabular; every list row is a 56 px
tap target with one primary action; empty states say what happens next; errors are sentences, never codes; no modal
dialogs except the Regenerate note; no raw provider fields; the Settings block is folded at the bottom; dark
only; `prefers-reduced-motion` turns animations off; focus rings on everything interactive; no spinner longer than a
second without a word beside it.

## done.log

One JSON object per line: `at`, `step` (`find`, `sync`, `gen` or `place`), `key`, `show`, `title`, `season`, `episode`,
`library`, `lang`, `result` (`found`, `none`, `rejected`, `queued`, `review`, `discarded`, `burned_in`, `failed`,
`interrupted`, `placed`, `deleted` or `shifted`), `why`, `source`, `confidence`, and `measured_ms` (sync lines only: the
agreed offset, or null). The file is rotated by size: when it passes 5 MiB the oldest half is dropped (place does it).
The board's `tonight` counts these lines since the current `night_id`.

## PC side

`subs_worker.py` runs inside a virtualenv on the GPU PC. It reads `job.json`, transcribes `audio.opus` with `large-v3`
(fp16, beam 5, VAD), writes `out.srt`, and computes `confidence` as the duration-weighted mean of `exp(avg_logprob)`
clamped to 0..1, plus `no_speech_ratio` and `language_detected`. It runs the text detector over the 12 frames
(`frames_with_text`), writes `result.json` and exits 0. It reads nothing outside its job folder, uses no network at run
time and holds no credential.

Cues are built from word timestamps, never from raw segments, because segments routinely run 20-30 s and sometimes past
60 s. `build_cues(words, max_cue_ms=7000, max_chars=42, max_lines=2, max_gap_ms=1500)` takes faster-whisper words in
order (`start`, `end` in seconds, `word`) and returns `(start_ms, end_ms, text)` cues. A cue closes at a sentence end
(`.` `!` `?` ending a word) once the cue holds at least 1 s, when adding the next word would exceed `max_lines ×
max_chars` characters (spaces counted, line breaks not), when the cue would exceed `max_cue_ms`, or when the gap between
a word's start and the previous word's end exceeds `max_gap_ms`. No cue exceeds `max_cue_ms`; a single word longer than
that is clipped. A cue's end is the last word's end, capped at the next cue's start and at least 200 ms after its start.
Cues are monotonic and never overlap; empty text is skipped; the text is wrapped by `_wrap_line` into at most
`max_lines` lines of at most `max_chars`. A cue that still needs more lines is split into several cues that share the
span in order, each getting time in proportion to its characters and never overlapping. A segment with `None` or empty
`words` is fed to the same function as one word spanning the segment. Confidence stays the duration-weighted mean over
segments, and the `no_speech_prob > 0.6` skip still applies per segment before its words are used. `progress.json` is
unchanged.

`probe.ps1` reports `nvidia-smi --query-gpu=utilization.gpu`, the `Get-Process` names and the hour as JSON. Nothing on the PC changes unless the owner runs `install-pc.ps1`. `tests/test_worker_cues.py` tests `build_cues` without a
model or network.

## Names the tests rely on

| module | names |
| --- | --- |
| `common.py` | `load_rules`, `log`, `redact`, `read_json`, `write_json`, `link_request`, `append_log`, `done`, `now`, `safe_media_path`, `lexical_media_path`, `sidecar_path`, `our_marker`, `parse_srt`, `write_srt`, `validate_subtitle`, `ass_to_srt`, `item_key`, `budget_take`, `mark`, `marks`, `decision`, `pc_idle`, `night_id`, `requests_pending`, `EXCLUDE_LIBRARIES`, `SECRET_RES`, the state constants |
| `scan.py` | `scan(db_path, rules) -> (library, coverage)`, `map_path(plex_path, rules)`, `parse_guids(rows)`, `main()` |
| `find.py` | `candidates(library, rules, marks_all, decisions_all, busy_keys, requests)`, `pick_best(results, rules)`, `os_login(client, env)`, `os_search(client, token, item, lang, env)`, `os_download(client, token, file_id, env)`, `main()`; the client is injectable through `client.request(method, url, headers, body, cap) -> (status, headers, body)` |
| `sync.py` | `apply_timing(cues, measured, spans, duration_ms, rules, regions) -> (ok, offset_ms \| None, cues, why)`, `shift_cues(cues, offset_ms)`, module attributes `speech_spans`, `measure_offset`, `load_detector`, `route(meta, cues, rules, es_kept)`, `process_one(key, lang, library, rules)`, `drop_placed(key, why)`, `main()` |
| `vad.py` | `load()`, `speech_spans(video, duration_ms) -> (spans, duration_ms, regions)`, `spans_from_probs(probs)`, `measure(cues, spans, duration_ms, regions) -> {offset_ms, halves, z, why}`, `regions_for(duration_ms)`, `Unavailable`, `SpeechError` |
| `gen.py` | `pick_item(library, rules, marks_all, decisions_all, busy_keys, requests)`, `extract(video, out_dir, rules, run)`, `validate_result(obj) -> dict \| None`, `mark_failure(key, why)`, `run_pc(key, run)`, `read_progress(remote, run)`, `main()`; `run(args, timeout)` is injectable |
| `place.py` | `place_one(key, lang, library, rules, env) -> (ok, why)`, `apply_decision(key, dec, library, rules, env)`, `own_sidecar(path, key, lang) -> bool`, `main()` |
| `server.py` | `Handler`, `state_payload()`, `current_payload(rules)`, `handle_pc(body)`, `missing_payload(q)`, `show_payload(sid)`, `handle_request(body)`, `handle_decision(body)` (keep, shift, regenerate, delete, exclude), `make_server(sock, group)`, `load_nav(path)`, `open_overlay(path)`, `overlay_bytes(name, fd)`, `serve()` |
