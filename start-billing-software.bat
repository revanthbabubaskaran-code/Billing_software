@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 goto missing_uv
uv run billing-software
exit /b %errorlevel%

:missing_uv
echo uv was not found. Install uv from https://docs.astral.sh/uv/ and run this file again.
pause
exit /b 1
