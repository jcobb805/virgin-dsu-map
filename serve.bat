@echo off
cd /d "%~dp0"
start http://localhost:8791/
python -m http.server 8791
