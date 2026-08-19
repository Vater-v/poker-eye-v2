@echo off
ssh root@5.42.124.216 "echo '=== services ==='; systemctl --no-pager --full status pokereye pokereye-web | sed -n '1,36p'; echo; echo '=== listeners ==='; ss -lntp | grep -E ':(19037|19100)\b' || true; echo; echo '=== latest ==='; journalctl -u pokereye -n 40 --no-pager"
