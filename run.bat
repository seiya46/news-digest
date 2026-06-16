@echo off
cd /d "%~dp0"
py fetch_news.py
if exist index.html (
    start "" "index.html"
)
pause
