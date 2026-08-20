@echo off
ssh root@5.42.124.216 "cd /opt/pokereye && python3 /opt/pokereye/vps/pool_status.py"
exit /b %ERRORLEVEL%
