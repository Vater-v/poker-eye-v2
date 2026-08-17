@echo off
setlocal
cd /d "%~dp0"
if "%POKEREYE_V2_SECRET%"=="" set "POKEREYE_V2_SECRET=change-me-local-only"
python main.py --secret "%POKEREYE_V2_SECRET%" %*
endlocal
