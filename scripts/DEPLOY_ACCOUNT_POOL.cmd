@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul || exit /b 2
set "ROOT=%CD%"
set "VPS=root@5.42.124.216"

for %%F in ("%ROOT%\core\v6router\accounts.py" "%ROOT%\vps\probe_eye_credential.py" "%ROOT%\vps\probe_coin_agent.sh" "%ROOT%\vps\pool_status.py" "%ROOT%\vps\hmn1_selftest.py") do (
  if not exist "%%~F" (
    echo [ERROR] Missing %%~F
    popd
    exit /b 2
  )
)

echo [PokerEye] Uploading account-pool hotfix...
scp "%ROOT%\core\v6router\accounts.py" "%VPS%:/tmp/pokereye-accounts.py"
if errorlevel 1 goto :fail
scp "%ROOT%\vps\probe_eye_credential.py" "%VPS%:/tmp/probe_eye_credential.py"
if errorlevel 1 goto :fail
scp "%ROOT%\vps\probe_coin_agent.sh" "%VPS%:/tmp/probe_coin_agent.sh"
if errorlevel 1 goto :fail
scp "%ROOT%\vps\pool_status.py" "%VPS%:/tmp/pool_status.py"
if errorlevel 1 goto :fail
scp "%ROOT%\vps\hmn1_selftest.py" "%VPS%:/tmp/hmn1_selftest.py"
if errorlevel 1 goto :fail

echo [PokerEye] Installing on VPS and restarting Trainer...
ssh "%VPS%" "set -e; mkdir -p /opt/pokereye/vps /var/backups/pokereye-pool; cp -a /opt/pokereye/core/v6router/accounts.py /var/backups/pokereye-pool/accounts.py.previous; install -m 0644 /tmp/pokereye-accounts.py /opt/pokereye/core/v6router/accounts.py; install -m 0755 /tmp/probe_eye_credential.py /opt/pokereye/vps/probe_eye_credential.py; install -m 0755 /tmp/probe_coin_agent.sh /opt/pokereye/vps/probe_coin_agent.sh; install -m 0755 /tmp/pool_status.py /opt/pokereye/vps/pool_status.py; install -m 0755 /tmp/hmn1_selftest.py /opt/pokereye/vps/hmn1_selftest.py; chown pokereye:pokereye /opt/pokereye/core/v6router/accounts.py /opt/pokereye/vps/probe_eye_credential.py /opt/pokereye/vps/probe_coin_agent.sh /opt/pokereye/vps/pool_status.py /opt/pokereye/vps/hmn1_selftest.py; cd /opt/pokereye; python3 -m py_compile core/v6router/accounts.py vps/probe_eye_credential.py vps/pool_status.py vps/hmn1_selftest.py; systemctl restart pokereye; sleep 1; systemctl is-active pokereye; if ! python3 /opt/pokereye/vps/hmn1_selftest.py; then echo '[ERROR] HMN1 selftest failed; rolling account pool back'; cp -a /var/backups/pokereye-pool/accounts.py.previous /opt/pokereye/core/v6router/accounts.py; chown pokereye:pokereye /opt/pokereye/core/v6router/accounts.py; systemctl restart pokereye; sleep 1; python3 /opt/pokereye/vps/hmn1_selftest.py; exit 1; fi; echo '[OK] account pool + HMN1 selftest passed'"
if errorlevel 1 goto :fail

echo.
echo [PokerEye] Pool hotfix installed.
echo [PokerEye] Read-only Coin agent probe: PROBE_COIN_AGENT.cmd
popd
exit /b 0

:fail
echo [ERROR] Account-pool deploy failed.
popd
exit /b 1
