@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul || exit /b 2
set "ROOT=%CD%"
set "VPS=root@5.42.124.216"
set "ARCHIVE=%TEMP%\pokereye-clear-deploy.tgz"
set "STAGE=%TEMP%\pokereye-clear-stage"

echo [PokerEye] CLEAR DEPLOY - no backups, no account autogrow.
echo [PokerEye] Applying safe account-runtime patch locally...
python "%ROOT%\tools\apply_clear_runtime_patch.py" "%ROOT%"
if errorlevel 1 goto :fail

rem Remove the backup produced by the broken previous patcher, if it exists.
if exist "%ROOT%\core\production_runtime.py.pre-clear.bak" del /Q "%ROOT%\core\production_runtime.py.pre-clear.bak"

copy /Y "%ROOT%\config\backend_accounts.clean.json" "%ROOT%\config\backend_accounts.local.json" >nul
if errorlevel 1 goto :fail

if exist "%STAGE%" rmdir /S /Q "%STAGE%"
mkdir "%STAGE%\core\v6router" "%STAGE%\core\verified_v1" "%STAGE%\config" "%STAGE%\vps" >nul
copy /Y "%ROOT%\core\v6router\accounts.py" "%STAGE%\core\v6router\accounts.py" >nul
copy /Y "%ROOT%\core\verified_v1\eye_direct_proxy.py" "%STAGE%\core\verified_v1\eye_direct_proxy.py" >nul
copy /Y "%ROOT%\core\production_runtime.py" "%STAGE%\core\production_runtime.py" >nul
copy /Y "%ROOT%\config\backend_accounts.clean.json" "%STAGE%\config\backend_accounts.clean.json" >nul
copy /Y "%ROOT%\vps\hmn1_selftest.py" "%STAGE%\vps\hmn1_selftest.py" >nul
copy /Y "%ROOT%\vps\probe_eye_credential.py" "%STAGE%\vps\probe_eye_credential.py" >nul
copy /Y "%ROOT%\vps\clean_recover.sh" "%STAGE%\vps\clean_recover.sh" >nul

if exist "%ARCHIVE%" del /Q "%ARCHIVE%"
tar -czf "%ARCHIVE%" -C "%STAGE%" .
if errorlevel 1 goto :fail

echo [PokerEye] Uploading clean recovery bundle...
scp "%ARCHIVE%" "%VPS%:/tmp/pokereye-clear-deploy.tgz"
if errorlevel 1 goto :fail

echo [PokerEye] Replacing broken registry/runtime on VPS...
rem IMPORTANT: use semicolon-separated remote commands; no CMD caret escaping.
ssh "%VPS%" "set -e; rm -rf /tmp/pokereye-clear; mkdir -p /tmp/pokereye-clear; tar -xzf /tmp/pokereye-clear-deploy.tgz -C /tmp/pokereye-clear; bash /tmp/pokereye-clear/vps/clean_recover.sh /tmp/pokereye-clear"
if errorlevel 1 goto :fail

echo.
echo [OK] CLEAR DEPLOY passed HMN1 self-test.
echo [OK] Known 7 restored as validated/AVAILABLE.
echo [OK] Synthetic autogrow remains OFF.
if exist "%ARCHIVE%" del /Q "%ARCHIVE%"
if exist "%STAGE%" rmdir /S /Q "%STAGE%"
popd
exit /b 0

:fail
echo [ERROR] CLEAR_DEPLOY failed. APK rebuild is NOT required.
popd
exit /b 1
