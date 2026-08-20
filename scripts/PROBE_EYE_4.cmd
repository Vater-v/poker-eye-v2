@echo off
setlocal
set "VPS=root@5.42.124.216"
echo [PokerEye] Read-only 4-way PokerEYE backend login probe.
echo [PokerEye] It uses currently FREE validated accounts and does not edit registry.
ssh "%VPS%" "cd /opt/pokereye && PYTHONPATH=/opt/pokereye python3 /opt/pokereye/vps/probe_parallel_eye.py --count 4 --timeout 10"
exit /b %ERRORLEVEL%
