@echo off
ssh root@5.42.124.216 "echo '=== TRAINER ==='; systemctl is-active pokereye; echo; echo '=== CONTROL ==='; curl -fsS http://127.0.0.1:19101/health; echo; echo; echo '=== LIVE STATE ==='; curl -fsS http://127.0.0.1:19101/snapshot | python3 -m json.tool"
exit /b %ERRORLEVEL%
