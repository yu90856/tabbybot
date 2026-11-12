@echo off
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist config.yaml copy config.example.yaml config.yaml
start "" http://127.0.0.1:8765
.venv\Scripts\python -m tabbybot gui
pause
