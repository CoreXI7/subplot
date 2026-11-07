# The GPU PC

Subplot sends audio and a few frames to a Windows PC with an NVIDIA GPU, which runs Whisper and sends back a subtitle.
The PC never receives a password, token or video file, and the server only uses it while it is idle by your rules.

## What it needs

- Windows 10 or 11 with an NVIDIA GPU (8 GB of VRAM or more) and a current driver.
- Python 3.12 from python.org (`py -3` works) and PowerShell 7.
- OpenSSH Server, with the server's service account allowed to log in by key.

## Install

1. Copy this `pc` folder to the PC.
2. In PowerShell 7, from that folder: `pwsh -NoProfile -ExecutionPolicy Bypass -File .\install-pc.ps1 -Root C:\Subplot`.
   It creates `C:\Subplot\venv`, installs the packages in `requirements.txt`, downloads the EAST text detector and checks
   it, then prints how many CUDA devices it sees.
3. On the server, as the service account: `ssh gpu-pc` must log in without a password, and `ssh -G gpu-pc` must show the
   PC's address as `hostname`. Use the same root as `PC_ROOT` in `subplot.conf` (with forward slashes: `C:/Subplot`).

## Test it by hand

Make a folder `C:\Subplot\jobs\test` holding:

- `audio.opus` from any short clip: `ffmpeg -i clip.mkv -vn -ac 1 -ar 16000 -c:a libopus -b:a 32k audio.opus`
- `job.json`: `{"lang": "en", "task": "transcribe", "model": "large-v3"}`
- a `frames` folder with a few JPEGs from the clip

Then run `C:\Subplot\venv\Scripts\python.exe C:\Subplot\pc\subs_worker.py C:\Subplot\jobs\test`. It writes `out.srt` and
`result.json` into that folder.

## When it runs

The idle rules live on the server in `rules.json` under `pc_idle`: the hours, processes that block a job (games are
detected by their install folder), the GPU use and free VRAM needed. The page's "Generate now" switch waives only the
hours.
