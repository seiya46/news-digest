@echo off
cd /d "%~dp0"
set PATH=%PATH%;C:\Program Files\Git\cmd
py fetch_news.py
if exist index.html (
    start "" "index.html"
)
git add index.html reports
git commit -m "Update news digest"
git push
pause
