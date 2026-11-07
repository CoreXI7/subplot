# Subplot worker install on the GPU PC. Run in PowerShell 7 from the folder holding this file:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File .\install-pc.ps1 -Root C:\Subplot
# Creates the venv, installs faster-whisper (CUDA ctranslate2 wheels) and OpenCV headless, fetches the EAST text model
# (sha256 checked), then runs the worker's import check. Whisper large-v3 (~3 GB) downloads on the first real job into
# <Root>\pc\models. Safe to run again.
param([string]$Root = 'C:\Subplot')
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path "$Root\pc", "$Root\jobs", "$Root\pc\models" | Out-Null
foreach ($f in 'subs_worker.py', 'probe.ps1', 'requirements.txt') {
  if ((Resolve-Path $PSScriptRoot).Path -ne (Resolve-Path "$Root\pc").Path) { Copy-Item -Force "$PSScriptRoot\$f" "$Root\pc\$f" }
}
if (-not (Test-Path "$Root\venv\Scripts\python.exe")) { py -3 -m venv "$Root\venv" }
& "$Root\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$Root\venv\Scripts\python.exe" -m pip install -r "$Root\pc\requirements.txt"
$east = "$Root\pc\models\frozen_east_text_detection.pb"
if (-not (Test-Path $east)) {
  Invoke-WebRequest -Uri 'https://github.com/oyyd/frozen_east_text_detection.pb/raw/master/frozen_east_text_detection.pb' -OutFile $east
}
$hash = (Get-FileHash $east -Algorithm SHA256).Hash.ToLower()
if ($hash -ne '9b486f3c3eee77b4c8cc91a83892c37026cca7d29b79bf3b93772ccd2db58454') {
  Remove-Item $east; throw "EAST model checksum mismatch ($hash); removed it. Run again, or download it by hand."
}
& "$Root\venv\Scripts\python.exe" -c "import faster_whisper, cv2, numpy; import ctranslate2; print('imports ok; CUDA devices:', ctranslate2.get_cuda_device_count())"
Write-Host "Subplot worker ready in $Root. The first job downloads Whisper large-v3 (~3 GB)."
