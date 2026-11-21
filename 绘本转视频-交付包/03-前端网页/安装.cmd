@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 goto done
python tools/install_ffmpeg.py
:done
pause
