@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Enter the project directory first. %~dp0 ends with a backslash; passing that
rem directly as a quoted PowerShell argument can turn the closing quote into
rem part of the value. %CD% after PUSHD has no trailing backslash here.
pushd "%~dp0" >nul || (
  echo [ERROR] Cannot enter project directory: %~dp0
  exit /b 2
)
set "ROOT=%CD%"

set "BUILD_ID=unknown"
if exist "%ROOT%\BUILD_ID" set /p BUILD_ID=<"%ROOT%\BUILD_ID"

echo [PokerEye] Building APK: %BUILD_ID%
echo [PokerEye] Root: %ROOT%
echo [PokerEye] Trainer: 5.42.124.216:19037 ^(VPS, IPv4^)
echo [PokerEye] Route: Android default / SocksDroid; ADB reverse: NOT USED

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\android\build_v2_native.ps1" -Repo "%ROOT%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [ERROR] APK build failed with code %RC%.
  popd
  exit /b %RC%
)

set "SRC=%ROOT%\.dist\v2workspace\out\coinpoker-production-native-debug.apk"
set "DST=%ROOT%\CoinPoker-PokerEye-%BUILD_ID%.apk"
if not exist "%SRC%" (
  echo [ERROR] Build finished but APK was not found: %SRC%
  popd
  exit /b 3
)
copy /Y "%SRC%" "%DST%" >nul
if errorlevel 1 (
  echo [ERROR] Could not copy APK to: %DST%
  popd
  exit /b 4
)

echo [PokerEye] APK: %DST%
certutil -hashfile "%DST%" SHA256
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
