# Changelog

## Unreleased

- Fixed: Plex's region tags (`en-us`, `en-gb`) on embedded subtitles now count as English, so those items are no
  longer looked up or generated.
- Fixed: on dual-audio files Whisper now hears the English track (the first one was used, often Japanese or Spanish).
- New check: a subtitle labelled English or Spanish must read as that language (alphabet and common words), or it is
  refused. It applies to found and generated files alike.
- Fixed: a cue that starts and ends at the same time is dropped instead of stopping the whole OpenSubtitles run; a file
  the writer refuses now fails only that file.
- Optional sign-in of its own (`AUTH=login`): one account with a password, an optional authenticator app with
  recovery codes, remembered browsers and a lock after 100 wrong tries, ported from Fishbowl. The reverse proxy then
  only needs to provide HTTPS. The default stays `AUTH=proxy`.

## First public version


- Scan Plex's library for items without English subtitles; find them on OpenSubtitles within a daily quota.
- Measure every found subtitle against the speech in the audio; shift it when it's off by up to a minute and measure
  again, drop it otherwise.
- Generate the rest with Whisper on a Windows GPU PC during your idle hours.
- Place subtitles beside the videos, marked as Subplot's, and replace or delete only Subplot's own files.
- A phone-first page with progress, what's missing, and the files that need a look.
- One sandboxed systemd service per step, rendered from one settings file by `install.sh`.
