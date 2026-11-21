param([int]$Port = 8765, [switch]$Quiet)
$root = $PSScriptRoot
$pidFile = Join-Path $root 'logs\server.pid'
$killed = $false

if (Test-Path -LiteralPath $pidFile) {
  $saved = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($saved -match '^\d+$') {
    try { Stop-Process -Id ([int]$saved) -Force -ErrorAction Stop; $killed = $true } catch { }
  }
  Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
}

if (-not $killed) {
  $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($conn) {
    try { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop; $killed = $true } catch { }
  }
}

if ($Quiet) { if ($killed) { "stopped" } else { "not-running" }; exit 0 }
$msg = "当前没有在运行的服务。"
if ($killed) { $msg = "服务已停止。" }
try {
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show($msg, "纸书智伴", "OK", "Information") | Out-Null
} catch { }
