@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "VPS=vps-aktan"
if /I "%~1"=="" goto :help
if /I "%~1"=="status" goto :status
if /I "%~1"=="logs" goto :logs
if /I "%~1"=="pool" goto :pool
if /I "%~1"=="snapshot" goto :snapshot
if /I "%~1"=="ingress" goto :ingress
if /I "%~1"=="probe-coin" goto :probe_coin
if /I "%~1"=="probe-eye" goto :probe_eye
if /I "%~1"=="probe-known" goto :probe_known
goto :help

:status
ssh %VPS% "echo === services ===; systemctl --no-pager --full status pokereye pokereye-web | sed -n '1,36p'; echo; echo === listeners ===; ss -lntp | grep -E ':(19037|19100)\b' || true; echo; echo === latest ===; journalctl -u pokereye -n 40 --no-pager"
exit /b %ERRORLEVEL%

:logs
ssh %VPS% "journalctl -u pokereye -f -n 60"
exit /b %ERRORLEVEL%

:pool
ssh %VPS% "cd /opt/pokereye && python3 /opt/pokereye/vps/pool_status.py"
exit /b %ERRORLEVEL%

:snapshot
ssh %VPS% "echo === TRAINER ===; systemctl is-active pokereye; echo; echo === CONTROL ===; curl -fsS http://127.0.0.1:19101/health; echo; echo; echo === LIVE STATE ===; curl -fsS http://127.0.0.1:19101/snapshot | python3 -m json.tool"
exit /b %ERRORLEVEL%

:ingress
ssh %VPS% "echo === service ===; systemctl is-active pokereye; echo; echo === listener ===; ss -lntp | grep ':19037' || true; echo; echo === local HMN1 ===; python3 /opt/pokereye/vps/hmn1_selftest.py || true"
exit /b %ERRORLEVEL%

:probe_coin
ssh -t %VPS% "cd /opt/pokereye && bash /opt/pokereye/vps/probe_coin_agent.sh"
exit /b %ERRORLEVEL%

:probe_eye
ssh %VPS% "cd /opt/pokereye && PYTHONPATH=/opt/pokereye python3 /opt/pokereye/vps/probe_parallel_eye.py --count 4 --timeout 10"
exit /b %ERRORLEVEL%

:probe_known
ssh %VPS% "cd /opt/pokereye && export PYTHONPATH=/opt/pokereye && python3 /opt/pokereye/vps/probe_eye_credential.py --credential-file /opt/pokereye/secrets/eye.agent --include-invalid --limit 7"
exit /b %ERRORLEVEL%

:help
echo usage: scripts\vps.cmd status^|logs^|pool^|snapshot^|ingress^|probe-coin^|probe-eye^|probe-known
exit /b 2
