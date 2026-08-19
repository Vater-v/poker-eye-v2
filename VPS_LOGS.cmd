@echo off
ssh root@5.42.124.216 "journalctl -u pokereye -f -n 60"
