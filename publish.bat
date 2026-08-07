@echo off
REM One-time publish: creates the GitHub repo, pushes, enables Pages, opens the link.
cd /d "%~dp0"
echo Creating repo and pushing...
gh repo create virgin-dsu-map --public --source . --push --description "Weekly screen: US horizontal permits in never-produced units (virgin DSUs)"
if errorlevel 1 (
  echo.
  echo Repo create failed ^(may already exist^) - trying plain push...
  git push -u origin main
)
echo Enabling GitHub Pages...
gh api -X POST repos/jcobb805/virgin-dsu-map/pages -f "source[branch]=main" -f "source[path]=/"
echo Waiting 90 seconds for the first Pages build...
timeout /t 90 /nobreak >nul
start https://jcobb805.github.io/virgin-dsu-map/
echo Done. If the page shows 404, give it another minute and refresh.
pause
