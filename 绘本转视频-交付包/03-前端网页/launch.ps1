param([int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$pidFile = Join-Path $logDir 'server.pid'
$outLog = Join-Path $logDir 'server.log'
$errLog = Join-Path $logDir 'server.err.log'
$url = "http://127.0.0.1:$Port/"

function Show-Msg([string]$text, [string]$title = "纸书智伴") {
  try {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($text, $title, "OK", "Information") | Out-Null
  } catch { }
}

function Test-App([int]$p) {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$p/" -UseBasicParsing -TimeoutSec 3
    return ($r.StatusCode -eq 200) -and ($r.Content -match "纸书智伴")
  } catch { return $false }
}

function Test-Port([int]$p) {
  $c = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
  return [bool]$c
}

function Resolve-Python {
  foreach ($c in @("python", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
  }
  foreach ($p in @("F:\Anaconda\main\python.exe", "C:\Python313\python.exe", "C:\Python312\python.exe")) {
    if (Test-Path -LiteralPath $p) { return $p }
  }
  return $null
}

if (Test-App $Port) {
  if (-not $NoBrowser) { Start-Process $url }
  exit 0
}
if (Test-Port $Port) {
  # 端口有人在听：可能是本应用正在启动中，等它一会儿再判断
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-App $Port) {
      if (-not $NoBrowser) { Start-Process $url }
      exit 0
    }
  }
  Show-Msg "端口 $Port 被别的程序占用了。请先关掉占用它的程序，或修改 launch.ps1 开头的 -Port。"
  exit 1
}

$python = Resolve-Python
if (-not $python) {
  Show-Msg "没找到 Python。请先双击 安装.cmd 安装运行环境。"
  exit 1
}

$proc = Start-Process -FilePath $python -ArgumentList @('-u', 'run.py', '--port', "$Port") -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
$proc.Id | Set-Content -LiteralPath $pidFile -Encoding ascii

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 500
  if ($proc.HasExited) { break }
  if (Test-App $Port) { $ready = $true; break }
}

if (-not $ready) {
  $tail = ""
  if (Test-Path -LiteralPath $errLog) { $tail = (Get-Content -LiteralPath $errLog -Tail 15 | Out-String).Trim() }
  if (Test-Path -LiteralPath $outLog) { $tail = $tail + " " + (Get-Content -LiteralPath $outLog -Tail 15 | Out-String).Trim() }
  Show-Msg ("启动失败：服务没能在 30 秒内响应。" + [Environment]::NewLine + [Environment]::NewLine + "日志：" + $errLog + [Environment]::NewLine + [Environment]::NewLine + $tail)
  exit 1
}

if (-not $NoBrowser) { Start-Process $url }
exit 0
