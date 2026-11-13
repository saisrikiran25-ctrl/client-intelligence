@echo off
cd /d "%~dp0"
echo Starting AI Agent Demos at http://localhost:8765
start "" http://localhost:8765
python server.py
pause
