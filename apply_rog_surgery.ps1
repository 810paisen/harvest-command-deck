# =====================================================================
# C:\Users\hidey\work\brain\apply_rog_surgery.ps1
# ROGDeskTop 外科手術：旧静的サーバーKill -> 新APIサーバー起動 -> agy常駐登録
# 実行(PowerShell 管理者): powershell -ExecutionPolicy Bypass -File .\apply_rog_surgery.ps1
# =====================================================================
$ErrorActionPreference = "Continue"

$Repo      = if ($env:ROG_BRAIN_REPO) { $env:ROG_BRAIN_REPO } else { "C:\Users\hidey\work\brain" }
$Port      = if ($env:HARVEST_PORT)   { $env:HARVEST_PORT }   else { "8799" }
$CdpPort   = if ($env:CDP_PORT)       { $env:CDP_PORT }       else { "9222" }
$Chrome    = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$Profile   = "C:\Users\hidey\chrome-harvest-profile"
$Agy       = "C:\Users\hidey\AppData\Local\Programs\Antigravity\bin\agy.cmd"
$Worker    = Join-Path $Repo "bin\rog_agy_worker.py"
$Server    = Join-Path $Repo "bin\harvest_api_server.py"
$TaskName  = "HarvestRogAgyWorker"

Write-Host "== [1/8] 旧ハリボテの停止 =="
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -and (
      $_.CommandLine -match "http\.server" -or
      $_.CommandLine -match "SimpleHTTPServer" -or
      $_.CommandLine -match "harvest_api_server" -or
      $_.CommandLine -match "rog_resident_patrol" -or
      $_.CommandLine -match "spawn_harvest_bots" -or
      $_.CommandLine -match "rog_agy_worker") } |
  ForEach-Object {
    Write-Host ("  kill pid={0} :: {1}" -f $_.ProcessId, $_.CommandLine.Substring(0,[Math]::Min(90,$_.CommandLine.Length)))
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }

Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Write-Host "  port $Port holder pid=$_ -> kill"; Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Write-Host "== [2/8] 必須ファイル確認 =="
foreach ($f in @($Worker, $Server)) {
  if (Test-Path $f) { Write-Host "  OK  $f" } else { Write-Host "  MISSING  $f" }
}
if (-not (Test-Path $Worker)) { Write-Host "  -> rog_agy_worker.py を配置してください"; }

Write-Host "== [3/8] git 同期 =="
if (Test-Path (Join-Path $Repo ".git")) {
  git -C $Repo fetch --all --prune
  git -C $Repo pull --rebase --autostash
  Write-Host ("  HEAD: " + (git -C $Repo rev-parse --short HEAD))
} else { Write-Host "  WARN: $Repo は git リポジトリではありません" }
foreach ($d in @("tasks","results","evidence")) {
  New-Item -ItemType Directory -Force -Path (Join-Path $Repo $d) | Out-Null
}

Write-Host "== [4/8] 新APIサーバー起動（旧静的サーバーは排除済） =="
if (Test-Path $Server) {
  Start-Process -FilePath "python" -ArgumentList @($Server, "--port", $Port) `
    -WorkingDirectory $Repo -WindowStyle Hidden
  Start-Sleep -Seconds 3
  try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 8
    Write-Host ("  /api/health OK : " + ($h | ConvertTo-Json -Compress))
  } catch { Write-Host "  WARN: /api/health 応答なし ($($_.Exception.Message))" }
} else { Write-Host "  WARN: $Server 不在 -> 8799は開きません" }

Write-Host "== [5/8] Chrome を CDP付きで起動 =="
$chromeRunning = $false
try { Invoke-RestMethod -Uri "http://127.0.0.1:$CdpPort/json/version" -TimeoutSec 5 | Out-Null; $chromeRunning = $true } catch {}
if ($chromeRunning) {
  Write-Host "  CDP は既に稼働中"
} elseif (Test-Path $Chrome) {
  New-Item -ItemType Directory -Force -Path $Profile | Out-Null
  Start-Process -FilePath $Chrome -ArgumentList @(
    "--remote-debugging-port=$CdpPort",
    "--user-data-dir=$Profile",
    "--no-first-run","--no-default-browser-check","--restore-last-session",
    "about:blank")
  Start-Sleep -Seconds 6
  Write-Host "  Chrome 起動（ログインはこのプロファイルで1回だけ手動実施）"
} else { Write-Host "  WARN: chrome.exe が見つかりません" }

Write-Host "== [6/8] agy 確認 =="
if (Test-Path $Agy) {
  Write-Host "  agy: $Agy"
  & $Agy --version 2>$null | Select-Object -First 1 | ForEach-Object { Write-Host "  $_" }
} else { Write-Host "  WARN: agy.cmd が見つかりません -> heuristics にフォールバック" }

Write-Host "== [7/8] 常駐タスク登録（サインイン時 + 5分ごと監視） =="
$py = (Get-Command python).Source
$action  = New-ScheduledTaskAction -Execute $py -Argument ("`"$Worker`"") -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set `
  -Description "Harvest ROG GUI worker (agy + real Chrome CDP, Grok Bot bypass)" -Force | Out-Null
Write-Host "  登録: $TaskName"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize

Write-Host "== [8/8] 疎通 =="
try {
  $s = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/state" -TimeoutSec 8
  Write-Host ("  actual_landed_yen={0} / submitted_pending_yen={1} / grok_quota={2}%" -f `
    $s.money.actual_landed_yen, $s.money.submitted_pending_yen, $s.quota.grok_bot_quota_pct)
} catch { Write-Host "  WARN: state 取得失敗 ($($_.Exception.Message))" }

Write-Host ""
Write-Host "ROG外科手術 完了。以後 Grok Bot を開かなくても自動で回ります。"
Write-Host "  ログ : $Repo\results\rog_worker.log"
Write-Host "  証跡 : $Repo\evidence\*.png"
Write-Host "  停止 : Stop-ScheduledTask -TaskName $TaskName"
