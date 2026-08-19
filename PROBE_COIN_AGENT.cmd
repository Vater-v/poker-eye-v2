@echo off
setlocal
ssh -t root@5.42.124.216 "cd /opt/pokereye && bash /opt/pokereye/vps/probe_coin_agent.sh"
exit /b %ERRORLEVEL%
