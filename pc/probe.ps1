$ErrorActionPreference='SilentlyContinue'
$q = (& nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1) -split ','
$gpu = [int](($q[0]) -replace '\D',''); $used = [int](($q[1]) -replace '\D',''); $total = [int](($q[2]) -replace '\D','')
$busy = @()
$pmon = & nvidia-smi pmon -c 1 -s u 2>$null
foreach ($line in $pmon) {
  if ($line -match '^\s*\d+\s+(\d+)\s+\S+\s+(\d+|-)\s+(\d+|-)\s+\S+\s+\S+\s+(\S+)\s*$') {
    $sm = $Matches[2]; $name = $Matches[4]
    if ($sm -ne '-' -and [int]$sm -ge 10) { $busy += $name }
  }
}
$procs = @(Get-Process | Select-Object -ExpandProperty ProcessName -Unique | ForEach-Object { "$_.exe" })
$games = @(Get-Process | Where-Object { $_.Path -like '*\steamapps\common\*' -or $_.Path -like '*\Oculus\Software\*' } | Select-Object -ExpandProperty ProcessName -Unique | ForEach-Object { "$_.exe" })
@{gpu_util=$gpu; gpu_mem_used_mb=$used; gpu_mem_total_mb=$total; busy_procs=@($busy | Select-Object -Unique); processes=$procs; game_procs=$games; hour=(Get-Date).Hour} | ConvertTo-Json -Compress
