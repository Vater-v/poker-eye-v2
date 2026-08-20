@echo off
setlocal
set "VPS=root@5.42.124.216"
echo [PokerEye] Rolling back only account-pool code; transport/UI are untouched.
ssh "%VPS%" "set -e; test -s /var/backups/pokereye-pool/accounts.py.previous; cp -a /var/backups/pokereye-pool/accounts.py.previous /opt/pokereye/core/v6router/accounts.py; chown pokereye:pokereye /opt/pokereye/core/v6router/accounts.py; python3 -m py_compile /opt/pokereye/core/v6router/accounts.py; systemctl restart pokereye; sleep 1; python3 /opt/pokereye/vps/hmn1_selftest.py; systemctl is-active pokereye"
