@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul || exit /b 2
set "ROOT=%CD%"
set "VPS=vps-aktan"
set "ARCHIVE=%TEMP%\pokereye-vps-deploy.tgz"

for %%F in ("%ROOT%\main.py" "%ROOT%\BUILD_ID" "%ROOT%\config\backend_accounts.local.json" "%ROOT%\secrets\trainer.secret" "%ROOT%\secrets\eye.agent" "%ROOT%\vps\install.sh") do (
  if not exist "%%~F" (
    echo [ERROR] Missing %%~F
    popd
    exit /b 2
  )
)
where tar >nul 2>nul || (echo [ERROR] tar.exe not found & popd & exit /b 2)
where scp >nul 2>nul || (echo [ERROR] scp.exe not found & popd & exit /b 2)
where ssh >nul 2>nul || (echo [ERROR] ssh.exe not found & popd & exit /b 2)

if exist "%ARCHIVE%" del /q "%ARCHIVE%"
echo [PokerEye] Packing VPS runtime...
tar -czf "%ARCHIVE%" -C "%ROOT%" main.py BUILD_ID core config/backend_accounts.local.json secrets/trainer.secret secrets/eye.agent vps
if errorlevel 1 (echo [ERROR] tar failed & popd & exit /b 3)

echo [PokerEye] Uploading to vps-aktan...
scp "%ARCHIVE%" "%VPS%:pokereye-vps-deploy.tgz"
if errorlevel 1 (echo [ERROR] scp failed & popd & exit /b 4)

echo [PokerEye] Installing on VPS (trainer restart only if no live/sitting tables)...
ssh "%VPS%" "set -e; rm -rf /tmp/pokereye-deploy; mkdir -p /tmp/pokereye-deploy; tar -xzf ~/pokereye-vps-deploy.tgz -C /tmp/pokereye-deploy; sudo -n bash /tmp/pokereye-deploy/vps/install.sh /tmp/pokereye-deploy"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (echo [ERROR] VPS install failed with %RC% & popd & exit /b %RC%)

echo.
echo [PokerEye] VPS deploy complete.
echo [PokerEye] Build APK locally with: BUILD_APK.cmd
echo [PokerEye] Then install with: INSTALL_APK.cmd emulator-5580
if exist "%ARCHIVE%" del /q "%ARCHIVE%"
popd
exit /b 0
