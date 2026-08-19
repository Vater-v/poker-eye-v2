@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul || exit /b 2
set "ROOT=%CD%"
set "VPS=root@5.42.124.216"
set "ARCHIVE=%TEMP%\pokereye-mtfix.tgz"

for %%F in ("%ROOT%\core\production_runtime.py" "%ROOT%\core\v6router\router.py" "%ROOT%\core\v6router\accounts.py" "%ROOT%\core\verified_v1\eye_direct_proxy.py" "%ROOT%\vps\pokereye-web.py" "%ROOT%\vps\install_multitable_fix.sh" "%ROOT%\vps\hmn1_selftest.py" "%ROOT%\vps\probe_parallel_eye.py") do (
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
echo [PokerEye] Packing MTABLE-20260818-A...
tar -czf "%ARCHIVE%" -C "%ROOT%" core/production_runtime.py core/v6router/router.py core/v6router/accounts.py core/verified_v1/eye_direct_proxy.py vps/pokereye-web.py vps/install_multitable_fix.sh vps/hmn1_selftest.py vps/probe_parallel_eye.py
if errorlevel 1 (echo [ERROR] tar failed & popd & exit /b 3)

echo [PokerEye] Uploading one bundle...
scp "%ARCHIVE%" "%VPS%:/tmp/pokereye-mtfix.tgz"
if errorlevel 1 (echo [ERROR] scp failed & popd & exit /b 4)

echo [PokerEye] Installing on VPS. No backups. Account registry is NOT reset.
ssh "%VPS%" "rm -rf /tmp/pokereye-mtfix && mkdir -p /tmp/pokereye-mtfix && tar -xzf /tmp/pokereye-mtfix.tgz -C /tmp/pokereye-mtfix && bash /tmp/pokereye-mtfix/vps/install_multitable_fix.sh /tmp/pokereye-mtfix"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [ERROR] multitable deploy failed with code %RC%
  popd
  exit /b %RC%
)

if exist "%ARCHIVE%" del /q "%ARCHIVE%"
echo.
echo [PokerEye] Done. APK rebuild/reinstall is NOT required.
echo [PokerEye] Open: https://admin.aktan.pro/pokereye/
popd
exit /b 0
