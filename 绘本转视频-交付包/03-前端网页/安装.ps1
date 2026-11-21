$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies failed to install' }
python tools/install_ffmpeg.py
if ($LASTEXITCODE -ne 0) { throw 'FFmpeg installation failed' }
Write-Host '安装完成。运行 启动.ps1 后打开 http://127.0.0.1:8765'
