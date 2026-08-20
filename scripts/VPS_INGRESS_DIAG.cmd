@echo off
setlocal
set "VPS=root@5.42.124.216"
echo [PokerEye] VPS HMN1 diagnostics. Leave CoinPoker running; APK is already retrying.
ssh "%VPS%" "echo '=== service ==='; systemctl is-active pokereye; systemctl --no-pager --full status pokereye | sed -n '1,14p'; echo; echo '=== listener ==='; ss -lntp | grep ':19037' || true; echo; echo '=== local HMN1 ==='; python3 /opt/pokereye/vps/hmn1_selftest.py || true; echo; echo '=== external packets for 12s ==='; if command -v tcpdump >/dev/null 2>&1; then timeout 12 tcpdump -ni any -nn -l 'tcp port 19037' 2>&1; else echo 'tcpdump not installed'; fi; echo; echo '=== trainer events ==='; f=$(find /opt/pokereye/logs -path '*/technical.log' -type f -printf '%%T@ %%p\n' | sort -nr | head -1 | cut -d' ' -f2-); echo $f; tail -n 80 $f | grep -E 'transport\.(hello|channel_up|disconnect)|trainer.ready' || true"
