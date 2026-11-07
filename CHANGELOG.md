# Changelog

## Unreleased

First public version.

- Scan Plex's library for items without English subtitles; find them on OpenSubtitles within a daily quota.
- Measure every found subtitle against the speech in the audio; shift it when it's off by up to a minute and measure
  again, drop it otherwise.
- Generate the rest with Whisper on a Windows GPU PC during your idle hours.
- Place subtitles beside the videos, marked as Subplot's, and replace or delete only Subplot's own files.
- A phone-first page with progress, what's missing, and the files that need a look.
- One sandboxed systemd service per step, rendered from one settings file by `install.sh`.
