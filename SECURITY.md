# Security

Subplot downloads subtitle files from strangers, sends audio to another machine, and writes beside your videos, so it
treats every subtitle, every provider reply and everything that comes back from the GPU PC as hostile. This page says how
to report a problem, what counts, and how each step is fenced in.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private report instead: the repository's **Security** tab →
**Report a vulnerability**. Include what you found, how to reproduce it, and which commit you ran. This is a small hobby
project: expect an answer within about a week, and if it's a real problem, a fix soon after. Please give it a chance to
ship before you write about it publicly.

## Which versions get fixes

Only the latest commit on `main`. Fixed problems get a GitHub Security Advisory.

## What's in scope

- the page and its API (served on the unix socket)
- subtitle parsing and validation, and anything a provider reply or a downloaded file can make Subplot do
- what the GPU PC can make the server do through the files it returns
- where files are written or deleted on the media drives
- `install.sh` and the units it renders

Especially interesting: anything that writes or deletes outside a video's own subtitle, touches a file Subplot didn't
write, gets a secret into a log, a state file, the page or the PC, or lets the downloader reach your network.

Out of scope: problems that need shell access as root or as the service account; running it without a reverse proxy that
signs you in, or with that proxy open to everyone; bugs in ffmpeg, Plex or other dependencies that aren't specific to how
Subplot uses them (report those upstream); scanner output with no explanation of impact.

## How it's fenced in

Each step is its own systemd service with `ProtectSystem=strict`, `ProtectHome=tmpfs`, no capabilities, a syscall filter,
memory and CPU limits, and only the folders it needs:

| Service | Media drives | Secrets | Network |
|---|---|---|---|
| scan | none | none | none |
| find | none | OpenSubtitles login and key | public internet only (LAN ranges and localhost blocked), HTTPS to `*.opensubtitles.com` |
| sync | read-only | none | none |
| place | read-write (the only one) | Plex token | 127.0.0.1 only (Plex) |
| gen | read-only | none | the GPU PC's address only (SSH) |
| board (the page) | none | none | none; a unix socket the proxy's group can open |

- **Writing beside videos.** Place opens each folder from the media root with `O_NOFOLLOW` (no symlink anywhere in the
  path), writes a temp file and renames it into place, and only ever replaces or deletes a subtitle whose last line carries
  Subplot's marker AND whose path, inode and sha256 match its own record. A file someone else wrote is never touched.
- **Untrusted files.** Every subtitle is validated before it is stored and again before it is placed: text encodings only,
  size and cue limits, monotonic times, tags other than italics and bold stripped. Untrusted media is parsed only in the
  sync and gen services, which hold no secrets and have no general network.
- **Secrets.** Read from one `.env` file (mode 600) by two services only, held in local variables, sent only in request
  headers, and redacted from every log line and stored message. The GPU PC never receives one.
- **The page.** It checks the `Host` header, needs a custom header and a same-origin request for every change, caps
  request bodies, and can only record decisions and requests; it can't name a path, a provider or a threshold. Signing in
  is your reverse proxy's job.
- **Installing.** `install.sh` accepts each setting only if it matches a strict pattern, so no value can inject systemd
  syntax; it refuses a root service account and checks every rendered unit with `systemd-analyze verify`.

## Known limits

- OpenSubtitles learns which titles you look up. The GPU PC receives your episodes' audio.
- The gate checks a subtitle's form and timing, not its wording.
- Generated subtitles are machine output; the confidence threshold and the "needs a look" list are the controls.
