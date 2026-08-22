@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "VPS=vps-aktan"
if /I "%~1"=="" goto :help
if /I "%~1"=="status" goto :status
if /I "%~1"=="logs" goto :logs
if /I "%~1"=="snapshot" goto :snapshot
goto :help

:status
ssh %VPS% "echo === services ===; systemctl --no-pager --full status pokereye pokereye-web | sed -n '1,36p'; echo; echo === listeners ===; ss -lntp | grep -E ':(19037|19100)\b' || true; echo; echo === latest ===; journalctl -u pokereye -n 40 --no-pager"
exit /b %ERRORLEVEL%

:logs
ssh %VPS% "journalctl -u pokereye -f -n 60"
exit /b %ERRORLEVEL%

:snapshot
ssh %VPS% "echo === TRAINER ===; systemctl is-active pokereye; echo; echo === CONTROL ===; curl -fsS http://127.0.0.1:19101/health; echo; echo; echo === LIVE STATE ===; curl -fsS http://127.0.0.1:19101/snapshot | python3 -m json.tool"
exit /b %ERRORLEVEL%

:help
echo usage: scripts\vps.cmd status^|logs^|snapshot
exit /b 2
