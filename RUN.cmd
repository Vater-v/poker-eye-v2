@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PYTHONDONTWRITEBYTECODE=1"

set "BUILD_ID=unknown"
if exist "%ROOT%BUILD_ID" set /p BUILD_ID=<"%ROOT%BUILD_ID"

echo [PokerEye] Trainer build: %BUILD_ID%
echo [PokerEye] Root: %ROOT%
echo [PokerEye] HMN1 server: plain IPv4 :19037; APK endpoint is VPS 5.42.124.216; ADB reverse is NOT used.

if not exist "%ROOT%main.py" (
  echo [ERROR] main.py not found: %ROOT%main.py
  exit /b 2
)
if "%POKEREYE_V2_SECRET%"=="" if not exist "%ROOT%secrets\trainer.secret" (
  echo [ERROR] Secret not found: %ROOT%secrets\trainer.secret
  echo [ERROR] Put trainer.secret there or set POKEREYE_V2_SECRET.
  exit /b 2
)
if not exist "%ROOT%config\backend_accounts.local.json" (
  echo [ERROR] Account file not found: %ROOT%config\backend_accounts.local.json
  exit /b 2
)
if not exist "%ROOT%secrets\eye.agent" (
  echo [ERROR] Credential file not found: %ROOT%secrets\eye.agent
  exit /b 2
)

python -B "%ROOT%main.py" ^
  --account-file "%ROOT%config\backend_accounts.local.json" ^
  --credential-file "%ROOT%secrets\eye.agent" ^
  --log-dir "%ROOT%logs" ^
  %*

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo [PokerEye] Trainer exited with code %RC%.
exit /b %RC%
