@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "BUILD_ID=unknown"
if exist "%ROOT%BUILD_ID" set /p BUILD_ID=<"%ROOT%BUILD_ID"
set "APK=%ROOT%CoinPoker-PokerEye-%BUILD_ID%.apk"
if not exist "%APK%" (
  echo [ERROR] APK not found: %APK%
  echo [ERROR] Run BUILD_APK.cmd first.
  exit /b 2
)
if "%~1"=="" (
  echo [PokerEye] Installing to the only/default adb device. No adb reverse is used.
  adb install -r "%APK%"
) else (
  echo [PokerEye] Installing to %~1. No adb reverse is used.
  adb -s "%~1" install -r "%APK%"
)
exit /b %ERRORLEVEL%
