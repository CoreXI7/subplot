# Testing Subplot

Subplot writes beside your videos and handles files from strangers, so its tests aim at the safety rules first: what may
be written or deleted, what a hostile file or reply can make it do, and where secrets may go.

## Run the tests yourself

From the checkout (no Plex, no GPU PC, no network, no media drives needed):

```
export SUBS_SANDBOX=$(mktemp -d -p "$HOME")
for t in tests/test_*.py; do python3 "$t" || break; done
venv/bin/python tests/test_vad.py          # the speech check needs numpy and onnxruntime
for a in tests/attack/*_attack.py; do SUBS_CODE=$PWD/subplot python3 "$a" | tail -1; done
```

`SUBS_SANDBOX` keeps test files out of `/tmp`; any folder you own works.

## What's tested, and how

| File | Tests | Covers |
|---|---|---|
| test_common.py | 22 | subtitle validation and parsing, the marker line, redaction of secrets, idle hours, safe media paths |
| test_find.py | 9 | the OpenSubtitles client: canonical URLs, download host rule, quota and throttling, secrets never in logs |
| test_pipeline.py | 26 | scan, sync and place end to end on a fake library: timing policy, routing, own-file rules, decisions |
| test_gen.py | 6 | picking what to generate, yielding to other GPU users, the idle-model unload rule |
| test_progress.py | 14 | the live progress row: what it may show, its expiry, path redaction |
| test_server.py | 17 | the board's API: host and origin checks, request limits, decisions only for files under review |
| test_board.py | 19 | the socket's mode, path refusal, `api/config`, header tabs, the overlay folder |
| test_board_seccomp.py | 1 | the real board starts and answers under the units' own syscall filter |
| test_install.py | 33 | every `subplot.conf` rule, unit rendering, `--check` |
| test_install_hardening.py | 10 | the installer's root-side guards: normal paths, no-follow folder creation, no taking over others' folders, units that can't ask for root |
| test_hook.py | 16 | the privacy hook on throwaway repositories: contents, paths, renames, binaries, names, history |
| test_vad.py | 11 | the speech measurement: planted offsets found with the right sign, disagreeing halves refused |
| test_marker_tag.py | 3 | a file is Subplot's only under the tag it was written with |
| test_worker_cues.py | 12 | the GPU PC's cue builder: length, line and gap limits |
| test_kick.py, test_more_a.py, test_more_b.py | 15 | a decision made during a run, shift arithmetic, edge cases |

The attack runners in `tests/attack/` throw hostile input at the real code and print one line per case:

| Runner | Cases | Throws |
|---|---|---|
| api_attack.py | 65 | malformed and cross-origin HTTP at the running board |
| find_attack.py | 77 | hostile OpenSubtitles replies: redirects, odd hosts, huge or broken bodies, bad ids |
| pc_attack.py | 95 | hostile results from the GPU PC and hostile names sent to it |
| place_attack.py | 16 | symlinks, FIFOs, foreign and forged files, traversal, decisions for the wrong key |
| srt_attack.py | 44 | malformed, oversized and slow-to-parse subtitles, marker tricks |

Each safety rule was also broken on purpose to prove a test notices: the socket opened to everyone, the overlay following
symlinks, a `%` accepted in a setting, the hook skipping author names, the double-slash check removed, generated files
shifted, a disabled check that the halves agree, the speech detector failing silently, and in the installer: non-normal
paths accepted, someone else's folder taken over, symlinks followed on the way to the state folder, a tampered unit
template rendered, and a chown in the board (which the syscall filter kills). Each made a test fail.

## What isn't covered

- A real Plex server, OpenSubtitles account and GPU PC. These are exercised by running Subplot, not by the test suite.
- The rendered units under a real systemd: `install.sh --check` runs `systemd-analyze verify`, but the sandbox itself is
  only proven on a live install. The installer's root-only steps (writing to `/etc/systemd/system`, the rollback on
  failure) run only there too.
- The PC worker's transcription (Whisper needs the GPU); only its cue building is tested.
